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
    use conexus_core::capability::Capability;
    use conexus_db::schema::init_router_schema;
    use std::collections::HashSet;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    /// A file-backed router DB opened as BOTH a `rusqlite::Connection`
    /// (this module's own `decide_add_group_member`/`decide_remove_
    /// group_member` under test stay rusqlite-based) and a sea-orm
    /// `DatabaseConnection` (for the now-converted `identity::
    /// grant_project_membership` fixture seed via
    /// [`seed_project_membership`]) -- same dual-connection recipe
    /// `identity.rs`'s own tests use, since an in-memory `:memory:` DB
    /// can't be shared across two separate connection handles the way
    /// a real file can.
    async fn conn_with_sea_orm() -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("admin_group_members_test.db");
        let c = Connection::open(&path).unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, c, db)
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

    /// test_sec_r2_admin_users.py Finding 1's group-indirection
    /// vector: nesting a GROUP the caller controls into a sysadmin-
    /// flagged group is the same escalation as joining directly (the
    /// nested group's own members would inherit sysadmin via the
    /// transitive closure) -- the guard checks the PARENT group's
    /// sysadmin status regardless of which member kind is being added,
    /// so this must be denied identically to the direct-user-join
    /// case above.
    #[test]
    fn a_non_sysadmin_cannot_nest_a_group_into_a_sysadmin_group() {
        let mut c = conn();
        let sysadmin_gid = group_membership_repository::create_group(&c, "real-admins", true, NOW)
            .unwrap()
            .group_id;
        let pawn_gid = seed_group(&c, "pawn-group");
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "bob",
            None,
            None,
            &sysadmin_gid,
            &serde_json::json!({"group_id": pawn_gid}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    /// test_sec_r2_admin_users.py Finding 1's transitive vector: the
    /// PARENT group itself is not flagged, but it's nested inside a
    /// sysadmin group -- joining the parent still inherits sysadmin
    /// via the resolver's transitive closure, so it must be denied
    /// identically.
    #[test]
    fn a_non_sysadmin_cannot_join_a_group_that_is_transitively_sysadmin() {
        let mut c = conn();
        let top_gid = group_membership_repository::create_group(&c, "top-admins", true, NOW)
            .unwrap()
            .group_id;
        let mid_gid = seed_group(&c, "middle-group");
        // mid ∈ top -- members of mid inherit sysadmin from top.
        group_membership_repository::add_group_member(&c, &top_gid, None, Some(&mid_gid), NOW)
            .unwrap();
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "bob",
            None,
            None,
            &mid_gid,
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

    fn principal_with_caps<'a, I: IntoIterator<Item = &'a str>>(
        user_id: &str,
        caps: I,
    ) -> Principal {
        use conexus_core::capability::Capabilities;
        use conexus_core::principal::PrincipalKind;
        let set: HashSet<Capability> = caps
            .into_iter()
            .map(|c| c.parse::<Capability>().unwrap())
            .collect();
        Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some(user_id.to_string()),
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities: Capabilities::Set(set),
        }
    }

    async fn seed_project_membership(
        db: &sea_orm::DatabaseConnection,
        project_name: &str,
        user_id: Option<&str>,
        group_id: Option<&str>,
        role: &str,
    ) {
        crate::identity::grant_project_membership(db, project_name, user_id, group_id, role)
            .await
            .unwrap();
    }

    // -- AZ-2 (test_sec_r4_cap_amplification.py): join-a-high-cap-group --

    #[test]
    fn a_delegate_can_join_a_group_whose_resolved_caps_are_all_held() {
        // test_sec_r4_cap_amplification.py's
        // `test_delegated_group_manager_can_join_held_cap_group`: the
        // guard only blocks amplification, not legitimate delegation.
        let mut c = conn();
        let gid = seed_group(&c, "g-lowcap");
        group_capability_replace(&c, &gid, ["system.groups.manage"]);
        let alice = seed_user(&mut c, "alice");
        let principal = principal_with_caps("bob", ["system.groups.manage"]);
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "bob",
            Some("bob"),
            Some(&principal),
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, AddGroupMemberOutcome::Added(_)));
    }

    #[test]
    fn a_sysadmin_can_add_a_member_to_a_high_cap_group() {
        // test_sec_r4_cap_amplification.py's
        // `test_sysadmin_can_add_member_to_high_cap_group`.
        let mut c = conn();
        let gid = seed_group(&c, "g-sys-highcap");
        group_capability_replace(&c, &gid, ["system.users.manage"]);
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "root",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, AddGroupMemberOutcome::Added(_)));
    }

    // -- AZ-R6-1 (test_sec_r6_groupjoin_membership.py): group-join
    // project-membership amplification -----------------------------------

    #[tokio::test]
    async fn a_delegate_cannot_join_themselves_into_a_project_member_group() {
        // A non-sysadmin holding only `system.groups.manage` with NO
        // membership on the victim project must not be able to add
        // THEMSELVES into a group that is an operator-member of it --
        // joining confers the project's operator role via the
        // resolver the data middleware gates on.
        let (_dir, mut c, db) = conn_with_sea_orm().await;
        let gid = seed_group(&c, "r6team");
        seed_project_membership(&db, "r6victim", None, Some(&gid), "operator").await;
        let alice = seed_user(&mut c, "alice");
        let principal = principal_with_caps(&alice, ["system.groups.manage"]);
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "alice",
            Some(&alice),
            Some(&principal),
            &gid,
            &serde_json::json!({"user_id": alice}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn a_delegate_cannot_add_a_controlled_group_into_a_project_member_group() {
        // Group-indirection: adding a GROUP (rather than a user)
        // member into the project-member group must be guarded the
        // same way -- the nested group (and anyone in it) would
        // transitively inherit the project's operator role.
        let (_dir, mut c, db) = conn_with_sea_orm().await;
        let gid = seed_group(&c, "r6team");
        seed_project_membership(&db, "r6victim", None, Some(&gid), "operator").await;
        let controlled = seed_group(&c, "g-controlled");
        let alice = seed_user(&mut c, "alice");
        let principal = principal_with_caps(&alice, ["system.groups.manage"]);
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "alice",
            Some(&alice),
            Some(&principal),
            &gid,
            &serde_json::json!({"group_id": controlled}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn a_sysadmin_can_add_a_member_to_a_project_member_group() {
        let (_dir, mut c, db) = conn_with_sea_orm().await;
        let gid = seed_group(&c, "r6team");
        seed_project_membership(&db, "r6victim", None, Some(&gid), "operator").await;
        let newbie = seed_user(&mut c, "newbie");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "root",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": newbie}),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, AddGroupMemberOutcome::Added(_)));
    }

    #[tokio::test]
    async fn an_operator_delegate_can_add_a_member_to_a_viewer_member_group() {
        // Regression: a delegate who holds OPERATOR on the project may
        // add a member into a group that is only a VIEWER-member of
        // that project -- conferring viewer is at or below their own
        // role.
        let (_dir, mut c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&mut c, "alice");
        seed_project_membership(&db, "r6victim", Some(&alice), None, "operator").await;
        let gid = seed_group(&c, "r6viewteam");
        seed_project_membership(&db, "r6victim", None, Some(&gid), "viewer").await;
        let newbie = seed_user(&mut c, "newbie2");
        let principal = principal_with_caps(&alice, ["system.groups.manage"]);
        let outcome = decide_add_group_member(
            &mut c,
            false,
            "alice",
            Some(&alice),
            Some(&principal),
            &gid,
            &serde_json::json!({"user_id": newbie}),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, AddGroupMemberOutcome::Added(_)));
    }

    // -- SD-R6-2: generic error on IntegrityError -------------------------

    #[test]
    fn a_foreign_key_violation_returns_a_generic_message_not_the_raw_sqlite_error() {
        let mut c = conn();
        let gid = seed_group(&c, "r6team");
        let outcome = decide_add_group_member(
            &mut c,
            true,
            "root",
            None,
            None,
            &gid,
            &serde_json::json!({"user_id": "no-such-user-id"}),
            NOW,
        )
        .unwrap();
        let AddGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        let message = body["message"].as_str().unwrap_or_default();
        assert!(!message.to_uppercase().contains("FOREIGN KEY"));
        assert!(!message.to_lowercase().contains("constraint"));
        assert_eq!(message, "could not add member");
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

    // -- AZ-R12-1 instance 2 (test_sec_r12_revoke_amplification.py):
    // removing a group member strips authority just as adding one
    // grants it -- the REMOVE path must run the SAME amplification
    // guard as ADD. -------------------------------------------------

    #[test]
    fn a_delegate_cannot_remove_a_member_from_a_cap_conferring_group() {
        // A delegate holding `system.groups.manage` but NOT
        // `system.users.manage` must not be able to remove a member
        // from a group that confers `system.users.manage` --
        // stripping a cap-conferring membership the delegate could
        // never grant.
        let mut c = conn();
        let gid = seed_group(&c, "g-caps");
        group_capability_replace(&c, &gid, ["system.users.manage"]);
        let victim = seed_user(&mut c, "victim");
        group_membership_repository::add_group_member(&c, &gid, Some(&victim), None, NOW).unwrap();
        let principal = principal_with_caps("alice", ["system.groups.manage"]);
        let outcome = decide_remove_group_member(
            &mut c,
            false,
            "alice",
            Some("alice"),
            Some(&principal),
            &gid,
            &victim,
        )
        .unwrap();
        let RemoveGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
        assert_eq!(
            group_membership_repository::group_member_count(&c, &gid).unwrap(),
            1
        );
    }

    #[tokio::test]
    async fn a_delegate_cannot_remove_a_member_from_a_project_role_group() {
        // The project-role vector: a delegate with NO role on the
        // victim project may not remove a member from a group that is
        // an operator-member of it -- stripping the project role the
        // delegate could never grant.
        let (_dir, mut c, db) = conn_with_sea_orm().await;
        let gid = seed_group(&c, "g-proj");
        seed_project_membership(&db, "proj-r12", None, Some(&gid), "operator").await;
        let victim = seed_user(&mut c, "victim");
        group_membership_repository::add_group_member(&c, &gid, Some(&victim), None, NOW).unwrap();
        let principal = principal_with_caps("alice", ["system.groups.manage"]);
        let outcome = decide_remove_group_member(
            &mut c,
            false,
            "alice",
            Some("alice"),
            Some(&principal),
            &gid,
            &victim,
        )
        .unwrap();
        let RemoveGroupMemberOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn a_delegate_can_remove_a_member_from_a_group_whose_caps_are_all_held() {
        // Regression: a delegate may remove a member from a group
        // whose conferred caps are all caps the delegate ALSO holds --
        // within their own authority to grant, so within their
        // authority to revoke.
        let mut c = conn();
        // R5-F4 fires on EVERY removal -- seed an unrelated real
        // sysadmin so the global invariant is genuinely satisfied and
        // this test isolates the amplification-guard mechanism alone.
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
        let gid = seed_group(&c, "g-safe");
        group_capability_replace(&c, &gid, ["system.users.manage"]);
        let victim = seed_user(&mut c, "victim");
        group_membership_repository::add_group_member(&c, &gid, Some(&victim), None, NOW).unwrap();
        let principal =
            principal_with_caps("alice", ["system.groups.manage", "system.users.manage"]);
        let outcome = decide_remove_group_member(
            &mut c,
            false,
            "alice",
            Some("alice"),
            Some(&principal),
            &gid,
            &victim,
        )
        .unwrap();
        assert!(matches!(outcome, RemoveGroupMemberOutcome::Removed(_)));
    }

    #[test]
    fn a_sysadmin_can_remove_a_member_from_a_cap_conferring_group() {
        let mut c = conn();
        // See the comment on the sibling test above -- R5-F4 needs an
        // unrelated real sysadmin seeded for a genuinely satisfied
        // global invariant.
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
        let gid = seed_group(&c, "g-caps");
        group_capability_replace(&c, &gid, ["system.users.manage"]);
        let victim = seed_user(&mut c, "victim");
        group_membership_repository::add_group_member(&c, &gid, Some(&victim), None, NOW).unwrap();
        let outcome =
            decide_remove_group_member(&mut c, true, "root", None, None, &gid, &victim).unwrap();
        assert!(matches!(outcome, RemoveGroupMemberOutcome::Removed(_)));
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
