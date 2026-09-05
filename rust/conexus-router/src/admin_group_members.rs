//! Decision functions for `admin_users_api.py`'s group-member
//! handlers (`list_group_members_handler`/
//! `add_group_member_handler`/`remove_group_member_handler`). Phase
//! E2, `conexus-router-admin-users-crud` (research item 9 of 10 --
//! the largest of the batch, unioning every amplification guard this
//! phase has built). Composes `admin_users_gate.rs`'s three
//! amplification guards (sysadmin-flag join, `system.*` capability
//! inheritance, project-role inheritance) with
//! `conexus-db::group_membership_repository`'s already-ported
//! writer/graph primitives.
//!
//! Framework-agnostic, matching every other decision-function module
//! this phase -- real axum route registration and the async
//! body-read yield point stay deferred to PR 23.

#![allow(dead_code)]

use conexus_core::principal::Principal;
use conexus_db::group_membership_repository::{self, GroupMemberRow, GroupMembershipError};
use rusqlite::{Connection, TransactionBehavior};

use crate::admin_users_gate::{self, AdminUsersError};
use crate::mcp_handler::HandlerResponse;

fn not_found_group(group_id: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("unknown group_id: {group_id:?}"),
        None,
    )
}

fn validation_rejected(message: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(AdminUsersError::Validation, message, None)
}

fn member_json(row: &GroupMemberRow) -> serde_json::Value {
    match row {
        GroupMemberRow::User {
            user_id,
            username,
            added_at,
        } => serde_json::json!({
            "user_id": user_id,
            "username": username,
            "added_at": added_at,
        }),
        GroupMemberRow::Group {
            group_id,
            name,
            is_sysadmin,
            added_at,
        } => serde_json::json!({
            "group_id": group_id,
            "name": name,
            "member_group_is_sysadmin": is_sysadmin,
            "added_at": added_at,
        }),
    }
}

/// Port of `list_group_members_handler`.
pub fn list_group_members_response(
    conn: &Connection,
    group_id: &str,
) -> rusqlite::Result<HandlerResponse> {
    if group_membership_repository::get_group(conn, group_id)?.is_none() {
        return Ok(not_found_group(group_id));
    }
    let members = group_membership_repository::list_group_members(conn, group_id)?;
    let json_members: Vec<serde_json::Value> = members.iter().map(member_json).collect();
    Ok(admin_users_gate::success_envelope(
        serde_json::json!({"members": json_members}),
        200,
    ))
}

/// Runs the three amplification guards `add_group_member_handler`/
/// `remove_group_member_handler` both share verbatim (a non-sysadmin
/// caller may not touch membership in a group that would confer
/// sysadmin, an unheld `system.*` capability, or a project role above
/// their own). `Ok(None)` means "allowed"; `Ok(Some(_))` is the
/// rejection to return.
fn amplification_guard(
    tx: &Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal: Option<&Principal>,
    parent_group_id: &str,
) -> rusqlite::Result<Option<HandlerResponse>> {
    if caller_is_sysadmin {
        return Ok(None);
    }
    if group_membership_repository::group_is_transitively_sysadmin(tx, parent_group_id)? {
        return Ok(Some(admin_users_gate::forbid_sysadmin_membership(
            caller_username,
        )));
    }
    let inherited = admin_users_gate::group_resolved_capabilities(tx, parent_group_id)?;
    let lacked =
        admin_users_gate::caps_caller_lacks(caller_is_sysadmin, caller_principal, &inherited);
    if !lacked.is_empty() {
        return Ok(Some(admin_users_gate::forbid_cap_amplification(
            caller_username,
            &lacked,
        )));
    }
    for (project, role) in
        group_membership_repository::group_resolved_project_roles(tx, parent_group_id)?
    {
        let caller_role = match caller_user_id {
            Some(uid) => {
                group_membership_repository::resolve_user_project_role(tx, uid, &project, None)?
            }
            None => None,
        };
        if let Some(resp) = admin_users_gate::membership_grant_denied(
            caller_is_sysadmin,
            caller_username,
            caller_role.as_deref(),
            &project,
            &role,
        ) {
            return Ok(Some(resp));
        }
    }
    Ok(None)
}

#[derive(Debug)]
pub enum AddGroupMemberOutcome {
    Added(serde_json::Value),
    Rejected(HandlerResponse),
}

/// Port of `add_group_member_handler`. Runs the shared amplification
/// guard + insert-time cycle detection + the INSERT inside ONE
/// `BEGIN IMMEDIATE` transaction (two concurrent adders can't each
/// pass the cycle check and then close a cycle between them).
#[allow(clippy::too_many_arguments)]
pub fn decide_add_group_member(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal: Option<&Principal>,
    parent_group_id: &str,
    raw_body: &serde_json::Value,
    now: &str,
) -> rusqlite::Result<AddGroupMemberOutcome> {
    let member_user_val = raw_body.get("user_id");
    let member_group_val = raw_body.get("group_id");
    // PF-R7-1: reject structured JSON types before the write lock.
    for (val, field) in [(member_user_val, "user_id"), (member_group_val, "group_id")] {
        if let Some(err) = admin_users_gate::reject_non_str(val, field, true) {
            return Ok(AddGroupMemberOutcome::Rejected(validation_rejected(&err)));
        }
    }
    let member_user_id = member_user_val.and_then(|v| v.as_str());
    let member_group_id = member_group_val.and_then(|v| v.as_str());
    if member_user_id.is_some() == member_group_id.is_some() {
        return Ok(AddGroupMemberOutcome::Rejected(validation_rejected(
            "exactly one of user_id or group_id is required",
        )));
    }

    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if group_membership_repository::get_group(&tx, parent_group_id)?.is_none() {
        return Ok(AddGroupMemberOutcome::Rejected(not_found_group(
            parent_group_id,
        )));
    }
    if let Some(resp) = amplification_guard(
        &tx,
        caller_is_sysadmin,
        caller_username,
        caller_user_id,
        caller_principal,
        parent_group_id,
    )? {
        return Ok(AddGroupMemberOutcome::Rejected(resp));
    }
    if let Some(child) = member_group_id {
        if group_membership_repository::would_create_cycle(&tx, parent_group_id, child)? {
            return Ok(AddGroupMemberOutcome::Rejected(
                admin_users_gate::error_envelope(
                    AdminUsersError::Conflict,
                    &format!(
                        "adding group {child:?} as a member of {parent_group_id:?} would close a \
                     cycle in the membership DAG"
                    ),
                    None,
                ),
            ));
        }
    }
    match group_membership_repository::add_group_member(
        &tx,
        parent_group_id,
        member_user_id,
        member_group_id,
        now,
    ) {
        Ok(()) => {}
        // Both already checked above -- unreachable in practice, kept
        // as a defensive fallthrough rather than `unreachable!()`.
        Err(GroupMembershipError::InvalidArgs) => {
            return Ok(AddGroupMemberOutcome::Rejected(validation_rejected(
                "exactly one of user_id or group_id is required",
            )))
        }
        Err(GroupMembershipError::CycleDetected { .. }) => {
            return Ok(AddGroupMemberOutcome::Rejected(
                admin_users_gate::error_envelope(
                    AdminUsersError::Conflict,
                    "would close a cycle in the membership DAG",
                    None,
                ),
            ))
        }
        Err(GroupMembershipError::Db(e)) => {
            if let rusqlite::Error::SqliteFailure(err, _) = &e {
                if err.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_UNIQUE {
                    return Ok(AddGroupMemberOutcome::Rejected(
                        admin_users_gate::error_envelope(
                            AdminUsersError::Conflict,
                            "membership already exists for this group + member",
                            None,
                        ),
                    ));
                }
                if err.code == rusqlite::ErrorCode::ConstraintViolation {
                    // SD-R6-2: a raw FK/CHECK message discloses schema
                    // details -- generic message, matching Python.
                    return Ok(AddGroupMemberOutcome::Rejected(validation_rejected(
                        "could not add member",
                    )));
                }
            }
            return Err(e);
        }
    }
    tx.commit()?;
    let mut member = serde_json::Map::new();
    if let Some(uid) = member_user_id {
        member.insert("user_id".to_string(), serde_json::json!(uid));
    }
    if let Some(gid) = member_group_id {
        member.insert("group_id".to_string(), serde_json::json!(gid));
    }
    Ok(AddGroupMemberOutcome::Added(serde_json::Value::Object(
        member,
    )))
}

#[derive(Debug)]
pub enum RemoveGroupMemberOutcome {
    Removed(String),
    Rejected(HandlerResponse),
}

/// Port of `remove_group_member_handler`. Runs the SAME amplification
/// guard as add (AZ-R12-1: removing a member strips authority just as
/// adding one grants it) plus the R5-F4 post-removal global-invariant
/// re-check, inside ONE transaction.
pub fn decide_remove_group_member(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal: Option<&Principal>,
    parent_group_id: &str,
    member_id: &str,
) -> rusqlite::Result<RemoveGroupMemberOutcome> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if let Some(resp) = amplification_guard(
        &tx,
        caller_is_sysadmin,
        caller_username,
        caller_user_id,
        caller_principal,
        parent_group_id,
    )? {
        return Ok(RemoveGroupMemberOutcome::Rejected(resp));
    }
    if !group_membership_repository::remove_group_member_by_id(&tx, parent_group_id, member_id)? {
        return Ok(RemoveGroupMemberOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::NotFound,
                &format!("no membership for member {member_id:?} in group {parent_group_id:?}"),
                None,
            ),
        ));
    }
    // R5-F4 vector 1: draining a sysadmin group's sole remaining live
    // member has the same end-state as clearing the flag or deleting
    // the group -- re-evaluate the GLOBAL invariant after the
    // removal, regardless of the amplification guard above (which a
    // sysadmin caller bypasses entirely).
    if admin_users_gate::no_sysadmin_would_remain(&tx)? {
        return Ok(RemoveGroupMemberOutcome::Rejected(
            admin_users_gate::last_sysadmin_error("remove"),
        ));
    }
    tx.commit()?;
    Ok(RemoveGroupMemberOutcome::Removed(member_id.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn seed_group(c: &Connection, name: &str) -> String {
        group_membership_repository::create_group(c, name, false, NOW)
            .unwrap()
            .group_id
    }

    fn seed_user(c: &mut Connection, username: &str) -> String {
        crate::identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .unwrap()
    }

    // -- list_group_members_response ------------------------------------

    #[test]
    fn lists_members_with_labels() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        let resp = list_group_members_response(&c, &gid).unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        let members = body["members"].as_array().unwrap();
        assert_eq!(members.len(), 1);
        assert_eq!(members[0]["username"], "alice");
    }

    #[test]
    fn rejects_listing_members_of_an_unknown_group() {
        let c = conn();
        let resp = list_group_members_response(&c, "nope").unwrap();
        assert_eq!(resp.status, 404);
    }

    // -- decide_add_group_member -----------------------------------------

    #[test]
    fn a_sysadmin_adds_a_user_member() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, AddGroupMemberOutcome::Added(_)));
        assert_eq!(
            group_membership_repository::group_member_count(&c, &gid).unwrap(),
            1
        );
    }

    #[test]
    fn rejects_neither_id_supplied() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            &gid,
            &serde_json::json!({}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_both_ids_supplied() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let other = seed_group(&c, "other");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": "alice", "group_id": other}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_adding_to_an_unknown_group() {
        let mut c = conn();
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            "nope",
            &serde_json::json!({"user_id": "alice"}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn rejects_a_duplicate_membership_as_conflict() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
    }

    #[test]
    fn rejects_a_group_edge_that_would_close_a_cycle() {
        let mut c = conn();
        let parent = seed_group(&c, "parent");
        let child = seed_group(&c, "child");
        group_membership_repository::add_group_member(&c, &parent, None, Some(&child), NOW)
            .unwrap();
        // child -> parent would close a 2-cycle.
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "admin",
            None,
            None,
            &child,
            &serde_json::json!({"group_id": parent}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
    }

    #[test]
    fn a_non_sysadmin_cannot_add_a_member_to_a_sysadmin_group() {
        let mut c = conn();
        let gid = group_membership_repository::create_group(&c, "engineers", true, NOW)
            .unwrap()
            .group_id;
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "bob",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn a_non_sysadmin_cannot_add_a_member_to_a_group_with_unheld_capabilities() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        group_capability_replace(&c, &gid, ["system.config.write"]);
        let alice = seed_user(&mut c, "alice");
        // No principal at all -- fails closed.
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "bob",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    fn group_capability_replace<'a, I: IntoIterator<Item = &'a str>>(
        c: &Connection,
        gid: &str,
        caps: I,
    ) {
        conexus_db::group_capability_repository::replace(c, gid, caps).unwrap();
    }

    // -- decide_remove_group_member --------------------------------------

    #[test]
    fn a_sysadmin_removes_a_user_member() {
        let mut c = conn();
        // R5-F4 fires on EVERY removal, not just from a sysadmin
        // group -- seed an unrelated real sysadmin so the global
        // invariant is genuinely satisfied and this test isolates the
        // actual mechanism under test, not the lockout.
        crate::identity::create_user(
            &mut c,
            "root",
            "correct horse battery staple",
            None,
            true,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let gid = seed_group(&c, "engineers");
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        let outcome =
            decide_remove_group_member(&mut c, true, "admin", None, None, &gid, &alice).unwrap();
        assert!(matches!(outcome, RemoveGroupMemberOutcome::Removed(_)));
        assert_eq!(
            group_membership_repository::group_member_count(&c, &gid).unwrap(),
            0
        );
    }

    #[test]
    fn rejects_removing_an_unknown_member() {
        let mut c = conn();
        let gid = seed_group(&c, "engineers");
        let outcome =
            decide_remove_group_member(&mut c, true, "admin", None, None, &gid, "nobody").unwrap();
        let RemoveGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn a_non_sysadmin_cannot_remove_a_member_from_a_sysadmin_group() {
        let mut c = conn();
        let gid = group_membership_repository::create_group(&c, "engineers", true, NOW)
            .unwrap()
            .group_id;
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        let outcome =
            decide_remove_group_member(&mut c, false, "bob", None, None, &gid, &alice).unwrap();
        let RemoveGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        // Confirm the rollback actually left the membership intact.
        assert_eq!(
            group_membership_repository::group_member_count(&c, &gid).unwrap(),
            1
        );
    }

    #[test]
    fn refuses_to_drain_the_last_sysadmin_groups_sole_member() {
        let mut c = conn();
        let gid = group_membership_repository::create_group(&c, "engineers", true, NOW)
            .unwrap()
            .group_id;
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        // Sysadmin caller bypasses the amplification guard but must
        // still hit the R5-F4 global-invariant check.
        let outcome =
            decide_remove_group_member(&mut c, true, "admin", None, None, &gid, &alice).unwrap();
        let RemoveGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        assert_eq!(
            group_membership_repository::group_member_count(&c, &gid).unwrap(),
            1,
            "the removal must have rolled back"
        );
    }

    #[test]
    fn allows_draining_when_another_sysadmin_source_remains() {
        let mut c = conn();
        let gid = group_membership_repository::create_group(&c, "engineers", true, NOW)
            .unwrap()
            .group_id;
        let alice = seed_user(&mut c, "alice");
        group_membership_repository::add_group_member(&c, &gid, Some(&alice), None, NOW).unwrap();
        crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            true,
            false,
            &[],
            NOW,
        )
        .unwrap();
        let outcome =
            decide_remove_group_member(&mut c, true, "admin", None, None, &gid, &alice).unwrap();
        assert!(matches!(outcome, RemoveGroupMemberOutcome::Removed(_)));
    }
}
