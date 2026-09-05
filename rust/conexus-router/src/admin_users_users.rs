//! Decision functions for `admin_users_api.py`'s user CRUD handlers
//! (`list_users_handler`/`create_user_handler`/`edit_user_handler`/
//! `delete_user_handler`). Phase E2, `conexus-router-admin-users-crud`
//! (research item 6 of 10) -- composes `admin_users_gate.rs`'s
//! validators/security-invariant checks + `identity.rs`'s public-row
//! primitives.
//!
//! Framework-agnostic and performs its own real DB writes directly,
//! matching `login.rs::attempt_setup`/`project_gate.rs`'s established
//! "decision function does the real write" precedent. The async
//! body-read yield point (`perm_gates.py`'s `read_body_and_revalidate`)
//! and real axum wiring stay deferred to PR 23.
//!
//! **A small, deliberate simplification from the literal Python
//! source**: `edit_user_handler` builds ONE dynamic `UPDATE ... SET
//! a = ?, b = ?` statement from a `sets`/`params` list assembled at
//! runtime. [`decide_edit_user`] instead runs up to two separate
//! single-column `UPDATE`s inside the SAME transaction -- functionally
//! identical (both fields still land atomically before `COMMIT`), and
//! avoids building SQL strings dynamically for a two-field surface.
#![allow(dead_code)]

use rusqlite::{Connection, OptionalExtension, TransactionBehavior};

use crate::admin_users_gate::{self, AdminUsersError};
use crate::identity::{self, IdentityError};
use crate::mcp_handler::HandlerResponse;

fn user_public_json(u: &identity::UserPublicRow) -> serde_json::Value {
    serde_json::json!({
        "user_id": u.user_id,
        "username": u.username,
        "email": u.email,
        "is_sysadmin": u.is_sysadmin,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    })
}

/// Port of `list_users_handler`.
pub fn list_users_response(conn: &Connection) -> Result<HandlerResponse, IdentityError> {
    let users = identity::list_users(conn)?;
    let json_users: Vec<serde_json::Value> = users.iter().map(user_public_json).collect();
    Ok(admin_users_gate::success_envelope(
        serde_json::json!({"users": json_users}),
        200,
    ))
}

#[derive(Debug)]
pub enum CreateUserOutcome {
    Created(identity::UserPublicRow),
    Rejected(HandlerResponse),
}

fn validation_rejected(message: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(AdminUsersError::Validation, message, None)
}

/// Port of `create_user_handler`. Argument extraction/validation
/// order matches the real Python source exactly (matters for which
/// error a multi-invalid-field body surfaces first).
pub fn decide_create_user(
    conn: &Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    raw_body: &serde_json::Value,
    now: &str,
) -> Result<CreateUserOutcome, IdentityError> {
    let username_val = raw_body.get("username");
    if let Some(err) = admin_users_gate::reject_non_str(username_val, "username", true) {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(&err)));
    }
    let username = username_val
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    // Port of `password = body.get("password") or ""`: any non-string
    // or falsy/empty value collapses to the SAME "password is
    // required" message Python's own `isinstance`+truthiness check
    // produces.
    let password = match raw_body.get("password") {
        Some(serde_json::Value::String(s)) if !s.is_empty() => s.clone(),
        _ => String::new(),
    };

    let email_val = raw_body.get("email");

    let (is_sysadmin, is_sysadmin_err) =
        admin_users_gate::parse_bool_field(raw_body.get("is_sysadmin"), "is_sysadmin", false);
    if let Some(err) = is_sysadmin_err {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(&err)));
    }

    // Granting sysadmin is sysadmin-only (self-escalation defence).
    if is_sysadmin && !caller_is_sysadmin {
        return Ok(CreateUserOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }

    if let Some(err) = admin_users_gate::validate_username(&username) {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(&err)));
    }
    if password.is_empty() {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(
            "password is required",
        )));
    }
    if let Err(IdentityError::WeakPassword(msg)) = identity::validate_password_strength(&password) {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(&msg)));
    }
    if let Some(err) = admin_users_gate::reject_non_str(email_val, "email", true) {
        return Ok(CreateUserOutcome::Rejected(validation_rejected(&err)));
    }
    let email = email_val.and_then(|v| v.as_str());

    match identity::admin_create_user(conn, &username, &password, email, is_sysadmin, now) {
        Ok(row) => Ok(CreateUserOutcome::Created(row)),
        Err(IdentityError::UsernameAlreadyExists(u)) => Ok(CreateUserOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::Conflict,
                &format!("username {u:?} already exists"),
                None,
            ),
        )),
        Err(e) => Err(e),
    }
}

#[derive(Debug)]
pub enum EditUserOutcome {
    Updated(identity::UserPublicRow),
    Rejected(HandlerResponse),
}

/// Port of `edit_user_handler`. Runs the last-sysadmin re-check and
/// the `UPDATE`(s) inside ONE `BEGIN IMMEDIATE` transaction so two
/// peers racing to demote the last two sysadmins can't both pass the
/// check (the loser sees the winner's write once it acquires the
/// write-lock and is rejected).
pub fn decide_edit_user(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    user_id: &str,
    raw_body: &serde_json::Value,
) -> Result<EditUserOutcome, IdentityError> {
    let has_is_sysadmin = raw_body.get("is_sysadmin").is_some();
    if has_is_sysadmin && !caller_is_sysadmin {
        return Ok(EditUserOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }

    let mut is_sysadmin_val = false;
    if has_is_sysadmin {
        let (v, err) =
            admin_users_gate::parse_bool_field(raw_body.get("is_sysadmin"), "is_sysadmin", false);
        if let Some(err) = err {
            return Ok(EditUserOutcome::Rejected(validation_rejected(&err)));
        }
        is_sysadmin_val = v;
    }

    let has_email = raw_body.get("email").is_some();
    let email_val: Option<String> = if has_email {
        if let Some(err) = admin_users_gate::reject_non_str(raw_body.get("email"), "email", true) {
            return Ok(EditUserOutcome::Rejected(validation_rejected(&err)));
        }
        raw_body
            .get("email")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    } else {
        None
    };

    if !has_is_sysadmin && !has_email {
        return Ok(EditUserOutcome::Rejected(validation_rejected(
            "no editable fields supplied",
        )));
    }

    let demoting = has_is_sysadmin && !is_sysadmin_val;

    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing_is_sysadmin: Option<bool> = tx
        .query_row(
            "SELECT is_sysadmin FROM users WHERE user_id = ?1",
            [user_id],
            |r| r.get(0),
        )
        .optional()?;
    let Some(existing_is_sysadmin) = existing_is_sysadmin else {
        return Ok(EditUserOutcome::Rejected(admin_users_gate::error_envelope(
            AdminUsersError::NotFound,
            &format!("unknown user_id: {user_id:?}"),
            None,
        )));
    };
    if demoting && existing_is_sysadmin && admin_users_gate::is_last_sysadmin(&tx, user_id)? {
        return Ok(EditUserOutcome::Rejected(
            admin_users_gate::last_sysadmin_error("demote"),
        ));
    }
    if has_is_sysadmin {
        tx.execute(
            "UPDATE users SET is_sysadmin = ?1 WHERE user_id = ?2",
            (is_sysadmin_val, user_id),
        )?;
    }
    if has_email {
        tx.execute(
            "UPDATE users SET email = ?1 WHERE user_id = ?2",
            (&email_val, user_id),
        )?;
    }
    let row = identity::get_user_public_by_id(&tx, user_id)?.expect("row confirmed to exist above");
    tx.commit()?;
    Ok(EditUserOutcome::Updated(row))
}

#[derive(Debug)]
pub enum DeleteUserOutcome {
    Deleted(String),
    Rejected(HandlerResponse),
}

/// Port of `delete_user_handler`. Runs the sysadmin-delete guard (a
/// delegate may not delete a sysadmin it can't demote, AZ-R9-1) and
/// the R5-F4 global-invariant re-check (evaluated AFTER the delete has
/// already cascaded away this user's `group_membership` rows) inside
/// ONE transaction.
pub fn decide_delete_user(
    conn: &mut Connection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    user_id: &str,
) -> Result<DeleteUserOutcome, IdentityError> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let existing_is_sysadmin: Option<bool> = tx
        .query_row(
            "SELECT is_sysadmin FROM users WHERE user_id = ?1",
            [user_id],
            |r| r.get(0),
        )
        .optional()?;
    let Some(existing_is_sysadmin) = existing_is_sysadmin else {
        return Ok(DeleteUserOutcome::Rejected(
            admin_users_gate::error_envelope(
                AdminUsersError::NotFound,
                &format!("unknown user_id: {user_id:?}"),
                None,
            ),
        ));
    };
    if existing_is_sysadmin && !caller_is_sysadmin {
        return Ok(DeleteUserOutcome::Rejected(
            admin_users_gate::forbid_sysadmin_write(caller_username),
        ));
    }
    tx.execute("DELETE FROM users WHERE user_id = ?1", [user_id])?;
    if admin_users_gate::no_sysadmin_would_remain(&tx)? {
        return Ok(DeleteUserOutcome::Rejected(
            admin_users_gate::last_sysadmin_error("delete"),
        ));
    }
    tx.commit()?;
    Ok(DeleteUserOutcome::Deleted(user_id.to_string()))
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

    fn seed_sysadmin(c: &mut Connection, username: &str) -> String {
        identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap()
    }

    // -- list_users_response ------------------------------------------

    #[test]
    fn list_users_response_omits_password_hash() {
        let mut c = conn();
        seed_sysadmin(&mut c, "alice");
        let resp = list_users_response(&c).unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        let users = body["users"].as_array().unwrap();
        assert_eq!(users.len(), 1);
        assert!(users[0].get("password_hash").is_none());
        assert_eq!(users[0]["username"], "alice");
    }

    // -- decide_create_user ------------------------------------------

    #[test]
    fn creates_a_user_with_default_non_sysadmin() {
        let c = conn();
        let outcome = decide_create_user(
            &c,
            false,
            "admin",
            &serde_json::json!({"username": "bob", "password": "correct horse battery staple"}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Created(row) = outcome else {
            panic!("expected Created, got {outcome:?}");
        };
        assert_eq!(row.username, "bob");
        assert!(!row.is_sysadmin);
    }

    #[test]
    fn a_non_sysadmin_cannot_mint_a_sysadmin() {
        let c = conn();
        let outcome = decide_create_user(
            &c,
            false,
            "admin",
            &serde_json::json!({"username": "bob", "password": "correct horse battery staple", "is_sysadmin": true}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn a_sysadmin_can_mint_a_sysadmin() {
        let c = conn();
        let outcome = decide_create_user(
            &c,
            true,
            "admin",
            &serde_json::json!({"username": "bob", "password": "correct horse battery staple", "is_sysadmin": true}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Created(row) = outcome else {
            panic!("expected Created, got {outcome:?}");
        };
        assert!(row.is_sysadmin);
    }

    #[test]
    fn rejects_a_missing_password() {
        let c = conn();
        let outcome = decide_create_user(
            &c,
            false,
            "admin",
            &serde_json::json!({"username": "bob"}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_a_weak_password() {
        let c = conn();
        let outcome = decide_create_user(
            &c,
            false,
            "admin",
            &serde_json::json!({"username": "bob", "password": "short"}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_a_duplicate_username_as_conflict() {
        let mut c = conn();
        seed_sysadmin(&mut c, "bob");
        let outcome = decide_create_user(
            &c,
            true,
            "admin",
            &serde_json::json!({"username": "bob", "password": "correct horse battery staple"}),
            NOW,
        )
        .unwrap();
        let CreateUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
    }

    // -- decide_edit_user ----------------------------------------------

    #[test]
    fn edits_the_email_field() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let outcome = decide_edit_user(
            &mut c,
            true,
            "admin",
            &uid,
            &serde_json::json!({"email": "alice@example.test"}),
        )
        .unwrap();
        let EditUserOutcome::Updated(row) = outcome else {
            panic!("expected Updated, got {outcome:?}");
        };
        assert_eq!(row.email.as_deref(), Some("alice@example.test"));
    }

    #[test]
    fn rejects_a_non_sysadmin_setting_is_sysadmin() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let outcome = decide_edit_user(
            &mut c,
            false,
            "admin",
            &uid,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        let EditUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn rejects_no_editable_fields() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let outcome =
            decide_edit_user(&mut c, true, "admin", &uid, &serde_json::json!({})).unwrap();
        let EditUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_editing_an_unknown_user() {
        let mut c = conn();
        let outcome = decide_edit_user(
            &mut c,
            true,
            "admin",
            "nobody",
            &serde_json::json!({"email": "a@b.test"}),
        )
        .unwrap();
        let EditUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn refuses_to_demote_the_last_sysadmin() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let outcome = decide_edit_user(
            &mut c,
            true,
            "admin",
            &uid,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        let EditUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        // Confirm the rollback actually took effect on disk.
        let row = identity::get_user_public_by_id(&c, &uid).unwrap().unwrap();
        assert!(row.is_sysadmin);
    }

    #[test]
    fn allows_demoting_when_another_sysadmin_remains() {
        let mut c = conn();
        let alice = seed_sysadmin(&mut c, "alice");
        identity::create_user(
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
        let outcome = decide_edit_user(
            &mut c,
            true,
            "admin",
            &alice,
            &serde_json::json!({"is_sysadmin": false}),
        )
        .unwrap();
        assert!(matches!(outcome, EditUserOutcome::Updated(_)));
    }

    // -- decide_delete_user ----------------------------------------------

    #[test]
    fn deletes_a_non_sysadmin_user() {
        let mut c = conn();
        seed_sysadmin(&mut c, "alice"); // first user, sysadmin -- keeps the invariant satisfied
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let outcome = decide_delete_user(&mut c, true, "admin", &bob).unwrap();
        assert!(matches!(outcome, DeleteUserOutcome::Deleted(_)));
        assert!(identity::get_user_public_by_id(&c, &bob).unwrap().is_none());
    }

    #[test]
    fn rejects_deleting_an_unknown_user() {
        let mut c = conn();
        let outcome = decide_delete_user(&mut c, true, "admin", "nobody").unwrap();
        assert!(matches!(outcome, DeleteUserOutcome::Rejected(_)));
    }

    #[test]
    fn a_non_sysadmin_cannot_delete_a_sysadmin() {
        let mut c = conn();
        let alice = seed_sysadmin(&mut c, "alice");
        identity::create_user(
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
        let outcome = decide_delete_user(&mut c, false, "bob", &alice).unwrap();
        let DeleteUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn refuses_to_delete_the_last_sysadmin() {
        let mut c = conn();
        let uid = seed_sysadmin(&mut c, "alice");
        let outcome = decide_delete_user(&mut c, true, "admin", &uid).unwrap();
        let DeleteUserOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        assert!(
            identity::get_user_public_by_id(&c, &uid).unwrap().is_some(),
            "the delete must have rolled back"
        );
    }
}
