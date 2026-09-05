//! Decision functions for `admin_users_api.py`'s group CRUD handlers
//! (`list_groups_handler`/`create_group_handler`/
//! `edit_group_handler`/`delete_group_handler`). Phase E2,
//! `conexus-router-admin-users-crud` (research item 7 of 10) --
//! composes `admin_users_gate.rs`'s validators/security-invariant
//! checks with `conexus-db::group_membership_repository`'s new
//! groups-CRUD primitives.
//!
//! Framework-agnostic, matching every other decision-function module
//! this phase -- real axum route registration and the async
//! body-read yield point stay deferred to PR 23.

#![allow(dead_code)]

use conexus_db::group_membership_repository::{self, GroupCrudError, GroupFieldUpdate, GroupRow};
use rusqlite::{Connection, TransactionBehavior};

use crate::admin_users_gate::{self, AdminUsersError};
use crate::mcp_handler::HandlerResponse;

fn group_public_json(group: &GroupRow, member_count: i64) -> serde_json::Value {
    serde_json::json!({
        "group_id": group.group_id,
        "name": group.name,
        "is_sysadmin": group.is_sysadmin,
        "created_at": group.created_at,
        "member_count": member_count,
    })
}

fn validation_rejected(message: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(AdminUsersError::Validation, message, None)
}

fn not_found(group_id: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("unknown group_id: {group_id:?}"),
        None,
    )
}

/// Port of `list_groups_handler`.
pub fn list_groups_response(conn: &Connection) -> rusqlite::Result<HandlerResponse> {
    let groups = group_membership_repository::list_groups_with_member_counts(conn)?;
    let json_groups: Vec<serde_json::Value> = groups
        .iter()
        .map(|(g, count)| group_public_json(g, *count))
        .collect();
    Ok(admin_users_gate::success_envelope(
        serde_json::json!({"groups": json_groups}),
        200,
    ))
}

#[derive(Debug)]
pub enum CreateGroupOutcome {
    Created(GroupRow),
    Rejected(HandlerResponse),
}

/// Port of `create_group_handler`. Argument extraction/validation
/// order matches the real Python source exactly.
pub fn decide_create_group(
    conn: &Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    raw_body: &serde_json::Value,
    now: &str,
) -> rusqlite::Result<CreateGroupOutcome> {
    let name_val = raw_body.get("name");
    if let Some(err) = admin_users_gate::reject_non_str(name_val, "name", true) {
        return Ok(CreateGroupOutcome::Rejected(validation_rejected(&err)));
    }
    let name = name_val
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    let (is_sysadmin, is_sysadmin_err) =
        admin_users_gate::parse_bool_field(raw_body.get("is_sysadmin"), "is_sysadmin", false);
    if let Some(err) = is_sysadmin_err {
        return Ok(CreateGroupOutcome::Rejected(validation_rejected(&err)));
    }

    // A sysadmin-flagged group confers sysadmin to its members, so
    // minting one is sysadmin-only (self-escalation defence).
    if is_sysadmin && !caller_is_sysadmin {
        return Ok(CreateGroupOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }

    if let Some(err) = admin_users_gate::validate_group_name(&name) {
        return Ok(CreateGroupOutcome::Rejected(validation_rejected(&err)));
    }

    match group_membership_repository::create_group(conn, &name, is_sysadmin, now) {
        Ok(group) => Ok(CreateGroupOutcome::Created(group)),
        Err(GroupCrudError::NameConflict) => Ok(CreateGroupOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::Conflict,
                &format!("group name {name:?} already exists"),
                None,
            ),
        )),
        Err(GroupCrudError::Db(e)) => Err(e),
    }
}

#[derive(Debug)]
pub enum EditGroupOutcome {
    Updated(GroupRow, i64),
    Rejected(HandlerResponse),
}

/// Port of `edit_group_handler`. Runs the R5-F4 last-sysadmin
/// re-check and the field update(s) inside ONE `BEGIN IMMEDIATE`
/// transaction, matching `admin_users_users.rs::decide_edit_user`'s
/// own atomicity precedent (two peers racing to clear the last two
/// sysadmin-granting groups can't both pass the check).
pub fn decide_edit_group(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    group_id: &str,
    raw_body: &serde_json::Value,
) -> rusqlite::Result<EditGroupOutcome> {
    let has_is_sysadmin = raw_body.get("is_sysadmin").is_some();
    if has_is_sysadmin && !caller_is_sysadmin {
        return Ok(EditGroupOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }

    let has_name = raw_body.get("name").is_some();
    let new_name: Option<String> = if has_name {
        if let Some(err) = admin_users_gate::reject_non_str(raw_body.get("name"), "name", true) {
            return Ok(EditGroupOutcome::Rejected(validation_rejected(&err)));
        }
        let name = raw_body
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if let Some(err) = admin_users_gate::validate_group_name(&name) {
            return Ok(EditGroupOutcome::Rejected(validation_rejected(&err)));
        }
        Some(name)
    } else {
        None
    };

    let mut is_sysadmin_val = false;
    if has_is_sysadmin {
        let (v, err) =
            admin_users_gate::parse_bool_field(raw_body.get("is_sysadmin"), "is_sysadmin", false);
        if let Some(err) = err {
            return Ok(EditGroupOutcome::Rejected(validation_rejected(&err)));
        }
        is_sysadmin_val = v;
    }

    if !has_name && !has_is_sysadmin {
        return Ok(EditGroupOutcome::Rejected(validation_rejected(
            "no editable fields supplied",
        )));
    }

    let demoting = has_is_sysadmin && !is_sysadmin_val;

    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let Some(existing) = group_membership_repository::get_group(&tx, group_id)? else {
        return Ok(EditGroupOutcome::Rejected(not_found(group_id)));
    };

    let update = GroupFieldUpdate {
        name: new_name.as_deref(),
        is_sysadmin: has_is_sysadmin.then_some(is_sysadmin_val),
    };
    match group_membership_repository::update_group_fields(&tx, group_id, &update) {
        Ok(true) => {}
        Ok(false) => return Ok(EditGroupOutcome::Rejected(not_found(group_id))),
        Err(GroupCrudError::NameConflict) => {
            return Ok(EditGroupOutcome::Rejected(
                admin_users_gate::error_envelope(
                    AdminUsersError::Conflict,
                    "group name already taken",
                    None,
                ),
            ))
        }
        Err(GroupCrudError::Db(e)) => return Err(e),
    }

    // R5-F4: re-evaluate the GLOBAL invariant AFTER the UPDATE has
    // already cleared the flag.
    if demoting && existing.is_sysadmin && admin_users_gate::no_sysadmin_would_remain(&tx)? {
        return Ok(EditGroupOutcome::Rejected(
            admin_users_gate::last_sysadmin_error("demote"),
        ));
    }

    let row = group_membership_repository::get_group(&tx, group_id)?
        .expect("row confirmed to exist above");
    let member_count = group_membership_repository::group_member_count(&tx, group_id)?;
    tx.commit()?;
    Ok(EditGroupOutcome::Updated(row, member_count))
}

#[derive(Debug)]
pub enum DeleteGroupOutcome {
    Deleted(String),
    Rejected(HandlerResponse),
}

/// Port of `delete_group_handler`. Gates on the group's TRANSITIVE
/// sysadmin status (AZ-R10-1/R5-F4, not just its own `is_sysadmin`
/// column) BEFORE the delete, then re-evaluates the global invariant
/// AFTER the delete has cascaded away this group's `group_membership`
/// rows -- both checks and the DELETE run inside ONE transaction.
pub fn decide_delete_group(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    group_id: &str,
) -> rusqlite::Result<DeleteGroupOutcome> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if group_membership_repository::get_group(&tx, group_id)?.is_none() {
        return Ok(DeleteGroupOutcome::Rejected(not_found(group_id)));
    }
    if !caller_is_sysadmin
        && group_membership_repository::group_is_transitively_sysadmin(&tx, group_id)?
    {
        return Ok(DeleteGroupOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }
    group_membership_repository::delete_group(&tx, group_id)?;
    if admin_users_gate::no_sysadmin_would_remain(&tx)? {
        return Ok(DeleteGroupOutcome::Rejected(
            admin_users_gate::last_sysadmin_error("delete"),
        ));
    }
    tx.commit()?;
    Ok(DeleteGroupOutcome::Deleted(group_id.to_string()))
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

    fn seed_sysadmin_group(c: &Connection) -> String {
        group_membership_repository::create_group(c, "engineers", true, NOW)
            .unwrap()
            .group_id
    }

    // -- list_groups_response --------------------------------------

    #[test]
    fn list_groups_response_includes_member_count() {
        let c = conn();
        seed_sysadmin_group(&c);
        let resp = list_groups_response(&c).unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        let groups = body["groups"].as_array().unwrap();
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0]["member_count"], 0);
        assert_eq!(groups[0]["name"], "engineers");
    }

    // -- decide_create_group -----------------------------------------

    #[test]
    fn creates_a_non_sysadmin_group_by_default() {
        let c = conn();
        let outcome = decide_create_group(
            &c,
            false,
            "admin",
            &serde_json::json!({"name": "team-a"}),
            NOW,
        )
        .unwrap();
        let CreateGroupOutcome::Created(group) = outcome else {
            panic!("expected Created, got {outcome:?}");
        };
        assert_eq!(group.name, "team-a");
        assert!(!group.is_sysadmin);
    }

    #[test]
    fn a_non_sysadmin_cannot_mint_a_sysadmin_group() {
        let c = conn();
        let outcome = decide_create_group(
            &c,
            false,
            "admin",
            &serde_json::json!({"name": "team-a", "is_sysadmin": true}),
            NOW,
        )
        .unwrap();
        let CreateGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn a_sysadmin_can_mint_a_sysadmin_group() {
        let c = conn();
        let outcome = decide_create_group(
            &c,
            true,
            "admin",
            &serde_json::json!({"name": "team-a", "is_sysadmin": true}),
            NOW,
        )
        .unwrap();
        let CreateGroupOutcome::Created(group) = outcome else {
            panic!("expected Created, got {outcome:?}");
        };
        assert!(group.is_sysadmin);
    }

    #[test]
    fn rejects_a_duplicate_group_name_as_conflict() {
        let c = conn();
        seed_sysadmin_group(&c);
        let outcome = decide_create_group(
            &c,
            true,
            "admin",
            &serde_json::json!({"name": "engineers"}),
            NOW,
        )
        .unwrap();
        let CreateGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
    }

    #[test]
    fn rejects_an_invalid_group_name() {
        let c = conn();
        let outcome =
            decide_create_group(&c, true, "admin", &serde_json::json!({"name": "!!!"}), NOW)
                .unwrap();
        let CreateGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    // -- decide_edit_group ---------------------------------------------

    #[test]
    fn edits_the_name_field() {
        let mut c = conn();
        let gid = seed_sysadmin_group(&c);
        let outcome = decide_edit_group(
            &mut c,
            true,
            "admin",
            &gid,
            &serde_json::json!({"name": "renamed"}),
        )
        .unwrap();
        let EditGroupOutcome::Updated(group, _) = outcome else {
            panic!("expected Updated, got {outcome:?}");
        };
        assert_eq!(group.name, "renamed");
    }

    #[test]
    fn rejects_a_non_sysadmin_setting_is_sysadmin() {
        let mut c = conn();
        let gid = seed_sysadmin_group(&c);
        let outcome = decide_edit_group(
            &mut c,
            false,
            "admin",
            &gid,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        let EditGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn rejects_no_editable_fields() {
        let mut c = conn();
        let gid = seed_sysadmin_group(&c);
        let outcome =
            decide_edit_group(&mut c, true, "admin", &gid, &serde_json::json!({})).unwrap();
        let EditGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_editing_an_unknown_group() {
        let mut c = conn();
        let outcome = decide_edit_group(
            &mut c,
            true,
            "admin",
            "nope",
            &serde_json::json!({"name": "x"}),
        )
        .unwrap();
        let EditGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn refuses_to_demote_the_last_sysadmin_group() {
        let mut c = conn();
        // No individual sysadmin USER exists -- this sysadmin-flagged
        // GROUP, with a real member, is the sole source of sysadmin in
        // the deployment (an EMPTY sysadmin group confers sysadmin to
        // nobody, so it wouldn't actually exercise this invariant).
        let gid = seed_sysadmin_group(&c);
        let alice = crate::identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .unwrap();
        conexus_db::group_membership_repository::add_group_member(
            &c,
            &gid,
            Some(&alice),
            None,
            NOW,
        )
        .unwrap();
        let outcome = decide_edit_group(
            &mut c,
            true,
            "admin",
            &gid,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        let EditGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        let row = group_membership_repository::get_group(&c, &gid)
            .unwrap()
            .unwrap();
        assert!(row.is_sysadmin, "the demotion must have rolled back");
    }

    #[test]
    fn allows_demoting_when_a_sysadmin_user_remains() {
        let mut c = conn();
        let gid = seed_sysadmin_group(&c);
        crate::identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            true,
            false,
            &[],
            NOW,
        )
        .unwrap();
        let outcome = decide_edit_group(
            &mut c,
            true,
            "admin",
            &gid,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        assert!(matches!(outcome, EditGroupOutcome::Updated(_, _)));
    }

    #[test]
    fn rejects_a_duplicate_name_on_edit_as_conflict() {
        let mut c = conn();
        group_membership_repository::create_group(&c, "taken", false, NOW).unwrap();
        let gid = group_membership_repository::create_group(&c, "other", false, NOW)
            .unwrap()
            .group_id;
        let outcome = decide_edit_group(
            &mut c,
            true,
            "admin",
            &gid,
            &serde_json::json!({"name": "taken"}),
        )
        .unwrap();
        let EditGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
    }

    // -- decide_delete_group -------------------------------------------

    #[test]
    fn deletes_a_non_sysadmin_group() {
        let mut c = conn();
        // A real sysadmin USER, not just a sysadmin-flagged group with
        // no members -- an empty sysadmin group confers sysadmin to
        // nobody, so it wouldn't actually keep the global invariant
        // satisfied on its own.
        crate::identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            true,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let gid = group_membership_repository::create_group(&c, "team-a", false, NOW)
            .unwrap()
            .group_id;
        let outcome = decide_delete_group(&mut c, true, "admin", &gid).unwrap();
        assert!(matches!(outcome, DeleteGroupOutcome::Deleted(_)));
        assert!(group_membership_repository::get_group(&c, &gid)
            .unwrap()
            .is_none());
    }

    #[test]
    fn rejects_deleting_an_unknown_group() {
        let mut c = conn();
        let outcome = decide_delete_group(&mut c, true, "admin", "nope").unwrap();
        assert!(matches!(outcome, DeleteGroupOutcome::Rejected(_)));
    }

    #[test]
    fn a_non_sysadmin_cannot_delete_a_sysadmin_group() {
        let mut c = conn();
        let gid = seed_sysadmin_group(&c);
        let outcome = decide_delete_group(&mut c, false, "bob", &gid).unwrap();
        let DeleteGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn refuses_to_delete_the_last_sysadmin_group() {
        let mut c = conn();
        // A real member, not an empty sysadmin-flagged group -- see
        // refuses_to_demote_the_last_sysadmin_group's own comment.
        let gid = seed_sysadmin_group(&c);
        let alice = crate::identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .unwrap();
        conexus_db::group_membership_repository::add_group_member(
            &c,
            &gid,
            Some(&alice),
            None,
            NOW,
        )
        .unwrap();
        let outcome = decide_delete_group(&mut c, true, "admin", &gid).unwrap();
        let DeleteGroupOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        assert!(
            group_membership_repository::get_group(&c, &gid)
                .unwrap()
                .is_some(),
            "the delete must have rolled back"
        );
    }
}
