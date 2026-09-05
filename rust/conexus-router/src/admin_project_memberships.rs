//! Decision functions for `admin_users_api.py`'s project-membership
//! handlers (`list_project_memberships_handler`/
//! `add_project_membership_handler`/
//! `change_project_membership_role_handler`/
//! `delete_project_membership_handler`). Phase E2,
//! `conexus-router-admin-users-crud` (research item 10 of 10 -- the
//! LAST piece of the `admin_users_api.py` port). Composes
//! `project_gate.rs`'s `deny_cross_tenant_project_read` (R7-F1's
//! project-existence-oracle closer, already proven in PR17) with
//! `admin_users_gate.rs`'s `membership_grant_denied` and the
//! already-ported `identity.rs` project-membership primitives.
//!
//! Framework-agnostic, matching every other decision-function module
//! this phase -- real axum route registration and the async
//! body-read yield point (`perm_gates.py`'s
//! `read_body_and_revalidate`, which for these three handlers ALSO
//! carries `project_name` so its fused re-check covers the
//! membership half, not just capability -- R9-F3) stay deferred to
//! PR 23.

#![allow(dead_code)]

use conexus_db::group_membership_repository;
use rusqlite::Connection;

use crate::admin_users_gate::{self, AdminUsersError, MembershipKind};
use crate::identity::{self, ProjectMembershipRow};
use crate::mcp_handler::HandlerResponse;
use crate::project_gate::{self, CrossTenantOutcome, GateError};
use crate::project_registry::ProjectRegistry;

/// [`identity::IdentityError`] carries variants (username-conflict,
/// weak-password) that never arise from this module's own read/write
/// calls -- collapse the whole enum into `GateError::Db` rather than
/// widen `GateError` for cases that can't happen here.
fn identity_err_to_gate(e: identity::IdentityError) -> GateError {
    match e {
        identity::IdentityError::Db(inner) => GateError::Db(inner),
        other => GateError::Db(rusqlite::Error::InvalidParameterName(other.to_string())),
    }
}

fn validation_rejected(message: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(AdminUsersError::Validation, message, None)
}

fn unknown_project(project_name: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("unknown project: {project_name:?}"),
        None,
    )
}

fn no_such_membership(membership_id: &str, project_name: &str) -> HandlerResponse {
    admin_users_gate::error_envelope(
        AdminUsersError::NotFound,
        &format!("no membership for {membership_id:?} in project {project_name:?}"),
        None,
    )
}

fn membership_row_json(row: &ProjectMembershipRow) -> serde_json::Value {
    match row {
        ProjectMembershipRow::User {
            user_id,
            username,
            role,
        } => serde_json::json!({
            "user_id": user_id,
            "username": username,
            "role": role,
            "membership_id": format!("u:{user_id}"),
        }),
        ProjectMembershipRow::Group {
            group_id,
            name,
            role,
        } => serde_json::json!({
            "group_id": group_id,
            "name": name,
            "role": role,
            "membership_id": format!("g:{group_id}"),
        }),
    }
}

/// Port of `list_project_memberships_handler`. **Deliberately NOT**
/// built on `deny_cross_tenant_project_read` -- that shared helper's
/// sysadmin bypass runs BEFORE the existence probe (by design, for
/// its own real callers: add/change/delete fall through to a
/// downstream INSERT/UPDATE/DELETE that has no separate not-found
/// path of its own for a bogus project name). This handler's real
/// Python source checks existence FIRST, unconditionally -- even a
/// sysadmin gets 404 for a genuinely nonexistent project -- and only
/// THEN applies the sysadmin-bypass to the membership check (R3-F1:
/// a non-sysadmin caller with no resolved role gets the SAME uniform
/// 404, closing the 200-roster/404 existence differential).
pub fn decide_list_project_memberships(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_user_id: Option<&str>,
    project_name: &str,
) -> Result<HandlerResponse, GateError> {
    if registry.get(project_name)?.is_none() {
        return Ok(unknown_project(project_name));
    }
    if !caller_is_sysadmin {
        let has_role = match caller_user_id {
            Some(uid) => group_membership_repository::resolve_user_project_role(
                conn,
                uid,
                project_name,
                None,
            )?
            .is_some(),
            None => false,
        };
        if !has_role {
            return Ok(unknown_project(project_name));
        }
    }
    let rows =
        identity::list_project_memberships(conn, project_name).map_err(identity_err_to_gate)?;
    let json_rows: Vec<serde_json::Value> = rows.iter().map(membership_row_json).collect();
    Ok(admin_users_gate::success_envelope(
        serde_json::json!({"memberships": json_rows}),
        200,
    ))
}

#[derive(Debug)]
pub enum AddProjectMembershipOutcome {
    Added(serde_json::Value),
    Rejected(HandlerResponse),
}

/// Port of `add_project_membership_handler`.
#[allow(clippy::too_many_arguments)]
pub fn decide_add_project_membership(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal_role: Option<&str>,
    project_name: &str,
    raw_body: &serde_json::Value,
) -> Result<AddProjectMembershipOutcome, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(AddProjectMembershipOutcome::Rejected(unknown_project(
                project_name,
            )))
        }
        CrossTenantOutcome::Forbidden { .. } => unreachable!("min_role: None never forbids"),
        CrossTenantOutcome::Admit => {}
    }

    let user_val = raw_body.get("user_id");
    let group_val = raw_body.get("group_id");
    for (val, field) in [(user_val, "user_id"), (group_val, "group_id")] {
        if let Some(err) = admin_users_gate::reject_non_str(val, field, true) {
            return Ok(AddProjectMembershipOutcome::Rejected(validation_rejected(
                &err,
            )));
        }
    }
    let user_id = user_val.and_then(|v| v.as_str());
    let group_id = group_val.and_then(|v| v.as_str());
    if user_id.is_some() == group_id.is_some() {
        return Ok(AddProjectMembershipOutcome::Rejected(validation_rejected(
            "exactly one of user_id or group_id is required",
        )));
    }
    let role = raw_body
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("operator");
    if let Some(err) = admin_users_gate::validate_role(role) {
        return Ok(AddProjectMembershipOutcome::Rejected(validation_rejected(
            &err,
        )));
    }
    // caller_principal_role: the caller's OWN resolved role on
    // project_name (None if they have none) -- see
    // membership_grant_denied's own doc for why this is threaded
    // explicitly rather than resolved internally.
    if let Some(resp) = admin_users_gate::membership_grant_denied(
        caller_is_sysadmin,
        caller_username,
        caller_principal_role,
        project_name,
        role,
    ) {
        return Ok(AddProjectMembershipOutcome::Rejected(resp));
    }

    match identity::grant_project_membership(conn, project_name, user_id, group_id, role) {
        Ok(()) => {}
        Err(identity::IdentityError::Db(e)) => {
            if matches!(&e, rusqlite::Error::SqliteFailure(err, _) if err.code == rusqlite::ErrorCode::ConstraintViolation)
            {
                // SD-R6-2: don't reflect the raw constraint text.
                return Ok(AddProjectMembershipOutcome::Rejected(
                    admin_users_gate::error_envelope(
                        AdminUsersError::Conflict,
                        "could not add membership",
                        None,
                    ),
                ));
            }
            return Err(GateError::Db(e));
        }
        Err(e) => return Err(identity_err_to_gate(e)),
    }

    let mut out = serde_json::Map::new();
    out.insert("role".to_string(), serde_json::json!(role));
    if let Some(uid) = user_id {
        out.insert("user_id".to_string(), serde_json::json!(uid));
        out.insert(
            "membership_id".to_string(),
            serde_json::json!(format!("u:{uid}")),
        );
    }
    if let Some(gid) = group_id {
        out.insert("group_id".to_string(), serde_json::json!(gid));
        out.insert(
            "membership_id".to_string(),
            serde_json::json!(format!("g:{gid}")),
        );
    }
    Ok(AddProjectMembershipOutcome::Added(
        serde_json::Value::Object(out),
    ))
}

fn resolve_target(kind: MembershipKind, target_id: &str) -> (Option<&str>, Option<&str>) {
    match kind {
        MembershipKind::User => (Some(target_id), None),
        MembershipKind::Group => (None, Some(target_id)),
    }
}

#[derive(Debug)]
pub enum ChangeProjectMembershipRoleOutcome {
    Changed(serde_json::Value),
    Rejected(HandlerResponse),
}

/// Port of `change_project_membership_role_handler`. AZ-R12-1: the
/// caller must be authorised for BOTH the role they SET and the role
/// they STRIP (a viewer-delegate may not downgrade an operator, since
/// that's a near-equivalent lockout to the DELETE path it would
/// otherwise bypass) -- `membership_grant_denied` runs twice, once
/// per role.
#[allow(clippy::too_many_arguments)]
pub fn decide_change_project_membership_role(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal_role: Option<&str>,
    project_name: &str,
    membership_id: &str,
    raw_body: &serde_json::Value,
) -> Result<ChangeProjectMembershipRoleOutcome, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(ChangeProjectMembershipRoleOutcome::Rejected(
                unknown_project(project_name),
            ))
        }
        CrossTenantOutcome::Forbidden { .. } => unreachable!("min_role: None never forbids"),
        CrossTenantOutcome::Admit => {}
    }

    let Some((kind, target_id)) = admin_users_gate::split_membership_id(membership_id) else {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(
            validation_rejected(&format!(
                "membership_id must be 'u:<id>' or 'g:<id>'; got {membership_id:?}"
            )),
        ));
    };

    let Some(new_role) = raw_body.get("role").and_then(|v| v.as_str()) else {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(
            validation_rejected("role is required"),
        ));
    };
    if let Some(err) = admin_users_gate::validate_role(new_role) {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(
            validation_rejected(&err),
        ));
    }
    if let Some(resp) = admin_users_gate::membership_grant_denied(
        caller_is_sysadmin,
        caller_username,
        caller_principal_role,
        project_name,
        new_role,
    ) {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(resp));
    }

    let (user_id, group_id) = resolve_target(kind, target_id);
    let existing_role = identity::project_membership_role(conn, project_name, user_id, group_id)
        .map_err(identity_err_to_gate)?;
    let Some(existing_role) = existing_role else {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(
            no_such_membership(membership_id, project_name),
        ));
    };
    // AZ-R12-1: authorise the STRIPPED role too.
    if let Some(resp) = admin_users_gate::membership_grant_denied(
        caller_is_sysadmin,
        caller_username,
        caller_principal_role,
        project_name,
        &existing_role,
    ) {
        return Ok(ChangeProjectMembershipRoleOutcome::Rejected(resp));
    }

    identity::update_project_membership_role(conn, project_name, user_id, group_id, new_role)
        .map_err(identity_err_to_gate)?;

    let mut out = serde_json::Map::new();
    out.insert("role".to_string(), serde_json::json!(new_role));
    out.insert(
        "membership_id".to_string(),
        serde_json::json!(membership_id),
    );
    match kind {
        MembershipKind::User => {
            out.insert("user_id".to_string(), serde_json::json!(target_id));
        }
        MembershipKind::Group => {
            out.insert("group_id".to_string(), serde_json::json!(target_id));
        }
    }
    Ok(ChangeProjectMembershipRoleOutcome::Changed(
        serde_json::Value::Object(out),
    ))
}

#[derive(Debug)]
pub enum DeleteProjectMembershipOutcome {
    Deleted(String),
    Rejected(HandlerResponse),
}

/// Port of `delete_project_membership_handler`. AZ-R12-1 (revoke
/// mirror of the ADD-side guard): the role being revoked must be at
/// or below the caller's own.
#[allow(clippy::too_many_arguments)]
pub fn decide_delete_project_membership(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal_role: Option<&str>,
    project_name: &str,
    membership_id: &str,
) -> Result<DeleteProjectMembershipOutcome, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(DeleteProjectMembershipOutcome::Rejected(unknown_project(
                project_name,
            )))
        }
        CrossTenantOutcome::Forbidden { .. } => unreachable!("min_role: None never forbids"),
        CrossTenantOutcome::Admit => {}
    }

    let Some((kind, target_id)) = admin_users_gate::split_membership_id(membership_id) else {
        return Ok(DeleteProjectMembershipOutcome::Rejected(
            validation_rejected(&format!(
                "membership_id must be 'u:<id>' or 'g:<id>'; got {membership_id:?}"
            )),
        ));
    };
    let (user_id, group_id) = resolve_target(kind, target_id);
    let existing_role = identity::project_membership_role(conn, project_name, user_id, group_id)
        .map_err(identity_err_to_gate)?;
    let Some(existing_role) = existing_role else {
        return Ok(DeleteProjectMembershipOutcome::Rejected(
            no_such_membership(membership_id, project_name),
        ));
    };
    if let Some(resp) = admin_users_gate::membership_grant_denied(
        caller_is_sysadmin,
        caller_username,
        caller_principal_role,
        project_name,
        &existing_role,
    ) {
        return Ok(DeleteProjectMembershipOutcome::Rejected(resp));
    }

    identity::remove_project_membership(conn, project_name, user_id, group_id)
        .map_err(identity_err_to_gate)?;
    Ok(DeleteProjectMembershipOutcome::Deleted(
        membership_id.to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;
    use tempfile::TempDir;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }
    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn now_dt() -> chrono::DateTime<chrono::Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }

    fn registry_with(dir: &TempDir, project_name: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let workspace = dir.path().join(project_name);
        registry
            .register(
                project_name,
                &workspace.to_string_lossy(),
                "python",
                now_dt(),
            )
            .unwrap();
        registry
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

    // -- decide_list_project_memberships ---------------------------------

    #[test]
    fn a_sysadmin_lists_memberships_of_any_project() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "operator").unwrap();
        let resp = decide_list_project_memberships(&c, &registry, true, None, "proj-a").unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["memberships"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn rejects_listing_a_nonexistent_project() {
        let dir = TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let c = conn();
        let resp = decide_list_project_memberships(&c, &registry, true, None, "nope").unwrap();
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn a_non_member_sees_the_same_404_as_a_nonexistent_project() {
        // R3-F1: closes the existence oracle -- a real project the
        // caller has no membership on looks identical to a
        // nonexistent one.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let c = conn();
        let resp =
            decide_list_project_memberships(&c, &registry, false, Some("bob"), "proj-a").unwrap();
        assert_eq!(resp.status, 404);
    }

    // -- decide_add_project_membership -----------------------------------

    #[test]
    fn a_sysadmin_grants_a_user_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_project_membership(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"user_id": alice}),
        )
        .unwrap();
        let AddProjectMembershipOutcome::Added(payload) = outcome else {
            panic!("expected Added, got {outcome:?}");
        };
        assert_eq!(payload["role"], "operator");
    }

    #[test]
    fn a_non_member_granting_on_a_nonexistent_project_gets_the_uniform_404() {
        // Real Python design (see decide_add_project_membership's own
        // doc): the sysadmin bypass in deny_cross_tenant_project_read
        // runs BEFORE the existence probe, so a SYSADMIN caller
        // targeting a genuinely nonexistent project falls through to
        // the INSERT itself (which has no project-existence FK) --
        // only a NON-sysadmin, non-member caller gets the closed-
        // oracle 404 this function actually guards.
        let dir = TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let c = conn();
        let outcome = decide_add_project_membership(
            &c,
            &registry,
            false,
            "bob",
            Some("bob"),
            None,
            "nope",
            &serde_json::json!({"user_id": "alice"}),
        )
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn a_viewer_cannot_grant_operator_role() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let bob = seed_user(&mut c, "bob");
        identity::grant_project_membership(&c, "proj-a", Some(&bob), None, "viewer").unwrap();
        let alice = seed_user(&mut c, "alice");
        let outcome = decide_add_project_membership(
            &c,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &serde_json::json!({"user_id": alice, "role": "operator"}),
        )
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[test]
    fn rejects_a_duplicate_membership_as_conflict() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "operator").unwrap();
        let outcome = decide_add_project_membership(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"user_id": alice}),
        )
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
    }

    // -- decide_change_project_membership_role ----------------------------

    #[test]
    fn a_sysadmin_changes_a_role() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "viewer").unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "operator"}),
        )
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Changed(payload) = outcome else {
            panic!("expected Changed, got {outcome:?}");
        };
        assert_eq!(payload["role"], "operator");
    }

    #[test]
    fn rejects_changing_role_on_an_unknown_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let c = conn();
        let outcome = decide_change_project_membership_role(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            "u:nobody",
            &serde_json::json!({"role": "operator"}),
        )
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn a_viewer_cannot_downgrade_an_operator() {
        // AZ-R12-1: the STRIPPED role (operator) must also be
        // authorised, not just the new one (viewer).
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let bob = seed_user(&mut c, "bob");
        identity::grant_project_membership(&c, "proj-a", Some(&bob), None, "viewer").unwrap();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "operator").unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "viewer"}),
        )
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        // Confirm the reject actually left the role untouched.
        assert_eq!(
            identity::project_membership_role(&c, "proj-a", Some(&alice), None)
                .unwrap()
                .as_deref(),
            Some("operator")
        );
    }

    #[test]
    fn rejects_a_malformed_membership_id() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let c = conn();
        let outcome = decide_change_project_membership_role(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            "not-a-valid-id",
            &serde_json::json!({"role": "operator"}),
        )
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    // -- decide_delete_project_membership ----------------------------------

    #[test]
    fn a_sysadmin_deletes_a_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "operator").unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &format!("u:{alice}"),
        )
        .unwrap();
        assert!(matches!(
            outcome,
            DeleteProjectMembershipOutcome::Deleted(_)
        ));
        assert!(
            identity::project_membership_role(&c, "proj-a", Some(&alice), None)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn rejects_deleting_an_unknown_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let c = conn();
        let outcome = decide_delete_project_membership(
            &c, &registry, true, "admin", None, None, "proj-a", "u:nobody",
        )
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn a_viewer_cannot_revoke_an_operators_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let mut c = conn();
        let bob = seed_user(&mut c, "bob");
        identity::grant_project_membership(&c, "proj-a", Some(&bob), None, "viewer").unwrap();
        let alice = seed_user(&mut c, "alice");
        identity::grant_project_membership(&c, "proj-a", Some(&alice), None, "operator").unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &format!("u:{alice}"),
        )
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        assert!(
            identity::project_membership_role(&c, "proj-a", Some(&alice), None)
                .unwrap()
                .is_some()
        );
    }
}
