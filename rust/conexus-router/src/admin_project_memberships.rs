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

/// Port of `list_project_memberships_handler`'s gate half.
/// **Deliberately NOT** built on `deny_cross_tenant_project_read` --
/// that shared helper's sysadmin bypass runs BEFORE the existence
/// probe (by design, for its own real callers: add/change/delete fall
/// through to a downstream INSERT/UPDATE/DELETE that has no separate
/// not-found path of its own for a bogus project name). This
/// handler's real Python source checks existence FIRST,
/// unconditionally -- even a sysadmin gets 404 for a genuinely
/// nonexistent project -- and only THEN applies the sysadmin-bypass
/// to the membership check (R3-F1: a non-sysadmin caller with no
/// resolved role gets the SAME uniform 404, closing the
/// 200-roster/404 existence differential).
///
/// Split from the actual `sea_orm`-backed list read (below) rather
/// than one combined `async fn` -- `conn: &Connection` is not `Send`
/// (`rusqlite::Connection` is deliberately not `Sync`), and an
/// `async fn` taking `&Connection` as its OWN parameter captures that
/// reference for its ENTIRE body, breaking axum's `Handler: Send`
/// bound for every REST handler that would await it (see
/// `project_gate::decide_create_project`'s own doc for the same
/// finding, empirically confirmed there -- every function in this
/// module hit it identically once converted). This SYNC gate takes
/// `conn`; [`finish_list_project_memberships`] below takes
/// `sea_orm_db` and does the actual (now-async) read -- the caller
/// (a REST handler, or a test) runs the gate, and only if it
/// `Proceed`s, awaits the finish half.
pub enum ListProjectMembershipsGate {
    Proceed,
    Rejected(HandlerResponse),
}

pub fn gate_list_project_memberships(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_user_id: Option<&str>,
    project_name: &str,
) -> Result<ListProjectMembershipsGate, GateError> {
    if registry.get(project_name)?.is_none() {
        return Ok(ListProjectMembershipsGate::Rejected(unknown_project(
            project_name,
        )));
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
            return Ok(ListProjectMembershipsGate::Rejected(unknown_project(
                project_name,
            )));
        }
    }
    Ok(ListProjectMembershipsGate::Proceed)
}

/// The `sea_orm`-backed read half of `list_project_memberships_handler`
/// -- call only after [`gate_list_project_memberships`] returns
/// `Proceed`. See that fn's own doc for why this is a separate
/// function.
pub async fn finish_list_project_memberships(
    sea_orm_db: &sea_orm::DatabaseConnection,
    project_name: &str,
) -> Result<HandlerResponse, GateError> {
    let rows = identity::list_project_memberships(sea_orm_db, project_name)
        .await
        .map_err(identity_err_to_gate)?;
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

/// Gate half of `add_project_membership_handler` -- see
/// [`gate_list_project_memberships`]'s own doc for why this is split
/// from the actual (now-async) grant write, done by
/// [`finish_add_project_membership`] below.
#[derive(Debug)]
pub enum AddProjectMembershipGate {
    Proceed {
        project_name: String,
        user_id: Option<String>,
        group_id: Option<String>,
        role: String,
    },
    Rejected(HandlerResponse),
}

#[allow(clippy::too_many_arguments)]
pub fn gate_add_project_membership(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal_role: Option<&str>,
    project_name: &str,
    raw_body: &serde_json::Value,
) -> Result<AddProjectMembershipGate, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(AddProjectMembershipGate::Rejected(unknown_project(
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
            return Ok(AddProjectMembershipGate::Rejected(validation_rejected(
                &err,
            )));
        }
    }
    let user_id = user_val.and_then(|v| v.as_str());
    let group_id = group_val.and_then(|v| v.as_str());
    if user_id.is_some() == group_id.is_some() {
        return Ok(AddProjectMembershipGate::Rejected(validation_rejected(
            "exactly one of user_id or group_id is required",
        )));
    }
    let role = raw_body
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("operator");
    if let Some(err) = admin_users_gate::validate_role(role) {
        return Ok(AddProjectMembershipGate::Rejected(validation_rejected(
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
        return Ok(AddProjectMembershipGate::Rejected(resp));
    }

    Ok(AddProjectMembershipGate::Proceed {
        project_name: project_name.to_string(),
        user_id: user_id.map(str::to_string),
        group_id: group_id.map(str::to_string),
        role: role.to_string(),
    })
}

/// The `sea_orm`-backed write half of `add_project_membership_handler`
/// -- call only after [`gate_add_project_membership`] returns
/// `Proceed`.
pub async fn finish_add_project_membership(
    sea_orm_db: &sea_orm::DatabaseConnection,
    project_name: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    role: &str,
) -> Result<AddProjectMembershipOutcome, GateError> {
    if let Err(e) =
        identity::grant_project_membership(sea_orm_db, project_name, user_id, group_id, role).await
    {
        // SD-R6-2: don't reflect the raw constraint text. Uses
        // `DbErr::sql_err`'s portable classification (works the same
        // across MySQL/Postgres/SQLite) rather than sniffing a
        // backend-specific error code -- see that method's own doc.
        if let identity::IdentityError::SeaOrm(db_err) = &e {
            if matches!(
                db_err.sql_err(),
                Some(sea_orm::SqlErr::UniqueConstraintViolation(_))
            ) {
                return Ok(AddProjectMembershipOutcome::Rejected(
                    admin_users_gate::error_envelope(
                        AdminUsersError::Conflict,
                        "could not add membership",
                        None,
                    ),
                ));
            }
        }
        return Err(identity_err_to_gate(e));
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

/// Gate half of `change_project_membership_role_handler` -- see
/// [`gate_list_project_memberships`]'s own doc for why this is split
/// from the actual (now-async) role change, done by
/// [`finish_change_project_membership_role`] below. AZ-R12-1: the
/// caller must be authorised for BOTH the role they SET and the role
/// they STRIP (a viewer-delegate may not downgrade an operator, since
/// that's a near-equivalent lockout to the DELETE path it would
/// otherwise bypass) -- `membership_grant_denied` runs twice, once
/// per role. Since the STRIP-side check needs the EXISTING role
/// (`identity::project_membership_role`, now `sea_orm`-backed), this
/// gate reads it via a THIRD parameter, `existing_role`, resolved by
/// the caller between the two halves (see
/// `finish_change_project_membership_role`'s own doc for why it's
/// arranged this way, not the other way around).
#[derive(Debug)]
pub enum ChangeProjectMembershipRoleGate {
    Proceed {
        project_name: String,
        membership_id: String,
        user_id: Option<String>,
        group_id: Option<String>,
        new_role: String,
    },
    Rejected(HandlerResponse),
}

#[allow(clippy::too_many_arguments)]
pub fn gate_change_project_membership_role(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_user_id: Option<&str>,
    caller_principal_role: Option<&str>,
    project_name: &str,
    membership_id: &str,
    raw_body: &serde_json::Value,
) -> Result<ChangeProjectMembershipRoleGate, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(ChangeProjectMembershipRoleGate::Rejected(unknown_project(
                project_name,
            )))
        }
        CrossTenantOutcome::Forbidden { .. } => unreachable!("min_role: None never forbids"),
        CrossTenantOutcome::Admit => {}
    }

    let Some((kind, target_id)) = admin_users_gate::split_membership_id(membership_id) else {
        return Ok(ChangeProjectMembershipRoleGate::Rejected(
            validation_rejected(&format!(
                "membership_id must be 'u:<id>' or 'g:<id>'; got {membership_id:?}"
            )),
        ));
    };

    let Some(new_role) = raw_body.get("role").and_then(|v| v.as_str()) else {
        return Ok(ChangeProjectMembershipRoleGate::Rejected(
            validation_rejected("role is required"),
        ));
    };
    if let Some(err) = admin_users_gate::validate_role(new_role) {
        return Ok(ChangeProjectMembershipRoleGate::Rejected(
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
        return Ok(ChangeProjectMembershipRoleGate::Rejected(resp));
    }

    let (user_id, group_id) = resolve_target(kind, target_id);
    Ok(ChangeProjectMembershipRoleGate::Proceed {
        project_name: project_name.to_string(),
        membership_id: membership_id.to_string(),
        user_id: user_id.map(str::to_string),
        group_id: group_id.map(str::to_string),
        new_role: new_role.to_string(),
    })
}

/// The `sea_orm`-backed half of `change_project_membership_role_handler`
/// -- call only after [`gate_change_project_membership_role`] returns
/// `Proceed`. Reads the CURRENT role, re-applies the AZ-R12-1
/// strip-side authorisation guard against it (the gate above already
/// applied the grant-side guard against `new_role`), then writes the
/// change -- both now genuinely `sea_orm`-backed reads/writes, so both
/// stay in this async half rather than splitting further.
#[allow(clippy::too_many_arguments)]
pub async fn finish_change_project_membership_role(
    sea_orm_db: &sea_orm::DatabaseConnection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_principal_role: Option<&str>,
    project_name: &str,
    membership_id: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
    new_role: &str,
) -> Result<ChangeProjectMembershipRoleOutcome, GateError> {
    let existing_role =
        identity::project_membership_role(sea_orm_db, project_name, user_id, group_id)
            .await
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

    identity::update_project_membership_role(sea_orm_db, project_name, user_id, group_id, new_role)
        .await
        .map_err(identity_err_to_gate)?;

    let mut out = serde_json::Map::new();
    out.insert("role".to_string(), serde_json::json!(new_role));
    out.insert(
        "membership_id".to_string(),
        serde_json::json!(membership_id),
    );
    if let Some(uid) = user_id {
        out.insert("user_id".to_string(), serde_json::json!(uid));
    }
    if let Some(gid) = group_id {
        out.insert("group_id".to_string(), serde_json::json!(gid));
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

/// Gate half of `delete_project_membership_handler` -- see
/// [`gate_list_project_memberships`]'s own doc for why this is split
/// from the actual (now-async) removal, done by
/// [`finish_delete_project_membership`] below.
#[derive(Debug)]
pub enum DeleteProjectMembershipGate {
    Proceed {
        project_name: String,
        membership_id: String,
        user_id: Option<String>,
        group_id: Option<String>,
    },
    Rejected(HandlerResponse),
}

/// Port of `delete_project_membership_handler`'s gate half. AZ-R12-1
/// (revoke mirror of the ADD-side guard): the role being revoked must
/// be at or below the caller's own -- but since that check needs the
/// EXISTING role (now `sea_orm`-backed), it moved into
/// [`finish_delete_project_membership`] below, alongside the removal
/// itself.
#[allow(clippy::too_many_arguments)]
pub fn gate_delete_project_membership(
    conn: &Connection,
    registry: &ProjectRegistry,
    caller_is_sysadmin: bool,
    caller_user_id: Option<&str>,
    project_name: &str,
    membership_id: &str,
) -> Result<DeleteProjectMembershipGate, GateError> {
    match project_gate::deny_cross_tenant_project_read(
        conn,
        registry,
        caller_is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::NotFound => {
            return Ok(DeleteProjectMembershipGate::Rejected(unknown_project(
                project_name,
            )))
        }
        CrossTenantOutcome::Forbidden { .. } => unreachable!("min_role: None never forbids"),
        CrossTenantOutcome::Admit => {}
    }

    let Some((kind, target_id)) = admin_users_gate::split_membership_id(membership_id) else {
        return Ok(DeleteProjectMembershipGate::Rejected(validation_rejected(
            &format!("membership_id must be 'u:<id>' or 'g:<id>'; got {membership_id:?}"),
        )));
    };
    let (user_id, group_id) = resolve_target(kind, target_id);
    Ok(DeleteProjectMembershipGate::Proceed {
        project_name: project_name.to_string(),
        membership_id: membership_id.to_string(),
        user_id: user_id.map(str::to_string),
        group_id: group_id.map(str::to_string),
    })
}

/// The `sea_orm`-backed half of `delete_project_membership_handler` --
/// call only after [`gate_delete_project_membership`] returns
/// `Proceed`.
#[allow(clippy::too_many_arguments)]
pub async fn finish_delete_project_membership(
    sea_orm_db: &sea_orm::DatabaseConnection,
    caller_is_sysadmin: bool,
    caller_username: &str,
    caller_principal_role: Option<&str>,
    project_name: &str,
    membership_id: &str,
    user_id: Option<&str>,
    group_id: Option<&str>,
) -> Result<DeleteProjectMembershipOutcome, GateError> {
    let existing_role =
        identity::project_membership_role(sea_orm_db, project_name, user_id, group_id)
            .await
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

    identity::remove_project_membership(sea_orm_db, project_name, user_id, group_id)
        .await
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

    /// A file-backed router DB opened as BOTH a `rusqlite::Connection`
    /// (to seed fixture rows / exercise the still-sync
    /// `registry`/`group_membership_repository` reads the `decide_*`
    /// functions here also make) and a sea-orm `DatabaseConnection`
    /// (for the now-converted `identity::project_membership`
    /// functions) -- same dual-connection recipe `identity.rs`'s own
    /// tests use, since an in-memory `:memory:` DB can't be shared
    /// across two separate connection handles the way a real file can.
    async fn conn_with_sea_orm() -> (TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("admin_project_memberships_test.db");
        let c = Connection::open(&path).unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, c, db)
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

    async fn seed_user(db: &sea_orm::DatabaseConnection, username: &str) -> String {
        crate::identity::create_user(
            db,
            username,
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW,
        )
        .await
        .unwrap()
    }

    // -- test-only "combined" wrappers -----------------------------------
    //
    // The PRODUCTION `gate_*`/`finish_*` split exists ONLY because a REAL
    // axum handler needs to stay `Send` (see `gate_list_project_
    // memberships`'s own doc) -- `#[tokio::test]`'s `block_on` has no
    // such requirement, so every test below keeps calling ONE combined
    // async fn with the SAME signature/behavior the pre-split
    // `decide_*` functions had, via these thin wrappers (never used as
    // an axum `Handler`, so their own non-`Send`-ness is harmless).

    async fn decide_list_project_memberships(
        conn: &Connection,
        sea_orm_db: &sea_orm::DatabaseConnection,
        registry: &ProjectRegistry,
        caller_is_sysadmin: bool,
        caller_user_id: Option<&str>,
        project_name: &str,
    ) -> Result<HandlerResponse, GateError> {
        match gate_list_project_memberships(
            conn,
            registry,
            caller_is_sysadmin,
            caller_user_id,
            project_name,
        )? {
            ListProjectMembershipsGate::Rejected(resp) => Ok(resp),
            ListProjectMembershipsGate::Proceed => {
                finish_list_project_memberships(sea_orm_db, project_name).await
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn decide_add_project_membership(
        conn: &Connection,
        sea_orm_db: &sea_orm::DatabaseConnection,
        registry: &ProjectRegistry,
        caller_is_sysadmin: bool,
        caller_username: &str,
        caller_user_id: Option<&str>,
        caller_principal_role: Option<&str>,
        project_name: &str,
        raw_body: &serde_json::Value,
    ) -> Result<AddProjectMembershipOutcome, GateError> {
        match gate_add_project_membership(
            conn,
            registry,
            caller_is_sysadmin,
            caller_username,
            caller_user_id,
            caller_principal_role,
            project_name,
            raw_body,
        )? {
            AddProjectMembershipGate::Rejected(resp) => {
                Ok(AddProjectMembershipOutcome::Rejected(resp))
            }
            AddProjectMembershipGate::Proceed {
                project_name,
                user_id,
                group_id,
                role,
            } => {
                finish_add_project_membership(
                    sea_orm_db,
                    &project_name,
                    user_id.as_deref(),
                    group_id.as_deref(),
                    &role,
                )
                .await
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn decide_change_project_membership_role(
        conn: &Connection,
        sea_orm_db: &sea_orm::DatabaseConnection,
        registry: &ProjectRegistry,
        caller_is_sysadmin: bool,
        caller_username: &str,
        caller_user_id: Option<&str>,
        caller_principal_role: Option<&str>,
        project_name: &str,
        membership_id: &str,
        raw_body: &serde_json::Value,
    ) -> Result<ChangeProjectMembershipRoleOutcome, GateError> {
        match gate_change_project_membership_role(
            conn,
            registry,
            caller_is_sysadmin,
            caller_username,
            caller_user_id,
            caller_principal_role,
            project_name,
            membership_id,
            raw_body,
        )? {
            ChangeProjectMembershipRoleGate::Rejected(resp) => {
                Ok(ChangeProjectMembershipRoleOutcome::Rejected(resp))
            }
            ChangeProjectMembershipRoleGate::Proceed {
                project_name,
                membership_id,
                user_id,
                group_id,
                new_role,
            } => {
                finish_change_project_membership_role(
                    sea_orm_db,
                    caller_is_sysadmin,
                    caller_username,
                    caller_principal_role,
                    &project_name,
                    &membership_id,
                    user_id.as_deref(),
                    group_id.as_deref(),
                    &new_role,
                )
                .await
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn decide_delete_project_membership(
        conn: &Connection,
        sea_orm_db: &sea_orm::DatabaseConnection,
        registry: &ProjectRegistry,
        caller_is_sysadmin: bool,
        caller_username: &str,
        caller_user_id: Option<&str>,
        caller_principal_role: Option<&str>,
        project_name: &str,
        membership_id: &str,
    ) -> Result<DeleteProjectMembershipOutcome, GateError> {
        match gate_delete_project_membership(
            conn,
            registry,
            caller_is_sysadmin,
            caller_user_id,
            project_name,
            membership_id,
        )? {
            DeleteProjectMembershipGate::Rejected(resp) => {
                Ok(DeleteProjectMembershipOutcome::Rejected(resp))
            }
            DeleteProjectMembershipGate::Proceed {
                project_name,
                membership_id,
                user_id,
                group_id,
            } => {
                finish_delete_project_membership(
                    sea_orm_db,
                    caller_is_sysadmin,
                    caller_username,
                    caller_principal_role,
                    &project_name,
                    &membership_id,
                    user_id.as_deref(),
                    group_id.as_deref(),
                )
                .await
            }
        }
    }

    // -- decide_list_project_memberships ---------------------------------

    #[tokio::test]
    async fn a_sysadmin_lists_memberships_of_any_project() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let resp = decide_list_project_memberships(&c, &db, &registry, true, None, "proj-a")
            .await
            .unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["memberships"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn rejects_listing_a_nonexistent_project() {
        let dir = TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let resp = decide_list_project_memberships(&c, &db, &registry, true, None, "nope")
            .await
            .unwrap();
        assert_eq!(resp.status, 404);
    }

    #[tokio::test]
    async fn a_non_member_sees_the_same_404_as_a_nonexistent_project() {
        // R3-F1: closes the existence oracle -- a real project the
        // caller has no membership on looks identical to a
        // nonexistent one.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let resp =
            decide_list_project_memberships(&c, &db, &registry, false, Some("bob"), "proj-a")
                .await
                .unwrap();
        assert_eq!(resp.status, 404);
    }

    /// R3-F1 regression guard: a non-sysadmin delegate WITH a
    /// resolved role on the project still gets the roster -- the
    /// scoping guard must not over-reject a legitimate member.
    #[tokio::test]
    async fn a_member_delegate_can_list_the_roster() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "viewer")
            .await
            .unwrap();
        let resp =
            decide_list_project_memberships(&c, &db, &registry, false, Some(&alice), "proj-a")
                .await
                .unwrap();
        let crate::mcp_handler::HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(resp.status, 200);
        assert!(body["memberships"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["user_id"] == alice));
    }

    // -- decide_add_project_membership -----------------------------------

    #[tokio::test]
    async fn a_sysadmin_grants_a_user_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"user_id": alice}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Added(payload) = outcome else {
            panic!("expected Added, got {outcome:?}");
        };
        assert_eq!(payload["role"], "operator");
    }

    /// PF-R7-1 (test_sec_r7_type_confusion.py): a structured JSON
    /// value (`dict`/`list`) in `user_id`/`group_id` must be a clean
    /// 400 `validation_error`, not an uncaught SQLite bind panic.
    #[tokio::test]
    async fn rejects_a_structured_user_id() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"user_id": {"nested": "obj"}}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 400);
    }

    #[tokio::test]
    async fn rejects_a_structured_group_id() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"group_id": ["list", "item"]}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 400);
    }

    #[tokio::test]
    async fn a_non_member_granting_on_a_nonexistent_project_gets_the_uniform_404() {
        // Real Python design (see decide_add_project_membership's own
        // doc): the sysadmin bypass in deny_cross_tenant_project_read
        // runs BEFORE the existence probe, so a SYSADMIN caller
        // targeting a genuinely nonexistent project falls through to
        // the INSERT itself (which has no project-existence FK) --
        // only a NON-sysadmin, non-member caller gets the closed-
        // oracle 404 this function actually guards.
        let dir = TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some("bob"),
            None,
            "nope",
            &serde_json::json!({"user_id": "alice"}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    /// R7-F1 (test_sec_r7_membership_write_oracle.py): the existence-
    /// oracle half of the fix -- an EXISTING project a non-member
    /// delegate has zero membership on must produce the SAME 404
    /// `not_found` shape as a genuinely nonexistent one (no 403-vs-404
    /// differential that would confirm the project is real).
    #[tokio::test]
    async fn a_non_member_on_an_existing_hidden_project_gets_the_same_404_as_nonexistent() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-hidden");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let hidden = decide_add_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some("bob"),
            None,
            "proj-hidden",
            &serde_json::json!({"user_id": "alice"}),
        )
        .await
        .unwrap();
        let empty_registry = ProjectRegistry::new(dir.path().join("empty.local.json"));
        let nonexistent = decide_add_project_membership(
            &c,
            &db,
            &empty_registry,
            false,
            "bob",
            Some("bob"),
            None,
            "no-such-slug-xyz",
            &serde_json::json!({"user_id": "alice"}),
        )
        .await
        .unwrap();
        let (
            AddProjectMembershipOutcome::Rejected(hidden_resp),
            AddProjectMembershipOutcome::Rejected(nonexistent_resp),
        ) = (hidden, nonexistent)
        else {
            panic!("expected both Rejected");
        };
        assert_eq!(hidden_resp.status, 404);
        assert_eq!(hidden_resp.status, nonexistent_resp.status);
    }

    #[tokio::test]
    async fn a_viewer_cannot_grant_operator_role() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let bob = seed_user(&db, "bob").await;
        identity::grant_project_membership(&db, "proj-a", Some(&bob), None, "viewer")
            .await
            .unwrap();
        let alice = seed_user(&db, "alice").await;
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &serde_json::json!({"user_id": alice, "role": "operator"}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }

    #[tokio::test]
    async fn rejects_a_duplicate_membership_as_conflict() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_add_project_membership(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &serde_json::json!({"user_id": alice}),
        )
        .await
        .unwrap();
        let AddProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
    }

    // -- decide_change_project_membership_role ----------------------------

    #[tokio::test]
    async fn a_sysadmin_changes_a_role() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "viewer")
            .await
            .unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "operator"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Changed(payload) = outcome else {
            panic!("expected Changed, got {outcome:?}");
        };
        assert_eq!(payload["role"], "operator");
    }

    /// R7-F1 sibling for `decide_change_project_membership_role`: a
    /// non-member's uniform 404 for an EXISTING (hidden) project, and
    /// no `system.projects.manage` role-rank/name leak in the body.
    #[tokio::test]
    async fn change_role_a_non_member_gets_the_uniform_404_for_an_existing_hidden_project() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-hidden");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let victim = seed_user(&db, "victim").await;
        identity::grant_project_membership(&db, "proj-hidden", Some(&victim), None, "operator")
            .await
            .unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some("bob"),
            None,
            "proj-hidden",
            &format!("u:{victim}"),
            &serde_json::json!({"role": "viewer"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        assert_eq!(
            identity::project_membership_role(&db, "proj-hidden", Some(&victim), None)
                .await
                .unwrap()
                .as_deref(),
            Some("operator"),
            "the unauthorized change must be a no-op"
        );
    }

    #[tokio::test]
    async fn rejects_changing_role_on_an_unknown_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            "u:nobody",
            &serde_json::json!({"role": "operator"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[tokio::test]
    async fn an_operator_delegate_can_downgrade_another_operator_to_viewer() {
        // test_sec_r12_revoke_amplification.py's
        // `test_operator_delegate_can_downgrade_operator_to_viewer`:
        // an operator-role delegate holds authority over BOTH the old
        // AND new role here, so the downgrade is within their own
        // authority -- must succeed, not be over-rejected by the
        // AZ-R12-1 guard added for the viewer-delegate case above.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let bob = seed_user(&db, "bob").await;
        identity::grant_project_membership(&db, "proj-a", Some(&bob), None, "operator")
            .await
            .unwrap();
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("operator"),
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "viewer"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Changed(payload) = outcome else {
            panic!("expected Changed, got {outcome:?}");
        };
        assert_eq!(payload["role"], "viewer");
    }

    #[tokio::test]
    async fn a_sysadmin_can_downgrade_an_operator_membership() {
        // test_sec_r12_revoke_amplification.py's
        // `test_sysadmin_can_downgrade_operator_membership`.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            true,
            "root",
            None,
            None,
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "viewer"}),
        )
        .await
        .unwrap();
        assert!(matches!(
            outcome,
            ChangeProjectMembershipRoleOutcome::Changed(_)
        ));
    }

    #[tokio::test]
    async fn a_viewer_cannot_downgrade_an_operator() {
        // AZ-R12-1: the STRIPPED role (operator) must also be
        // authorised, not just the new one (viewer).
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let bob = seed_user(&db, "bob").await;
        identity::grant_project_membership(&db, "proj-a", Some(&bob), None, "viewer")
            .await
            .unwrap();
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &format!("u:{alice}"),
            &serde_json::json!({"role": "viewer"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        // Confirm the reject actually left the role untouched.
        assert_eq!(
            identity::project_membership_role(&db, "proj-a", Some(&alice), None)
                .await
                .unwrap()
                .as_deref(),
            Some("operator")
        );
    }

    #[tokio::test]
    async fn rejects_a_malformed_membership_id() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_change_project_membership_role(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            "not-a-valid-id",
            &serde_json::json!({"role": "operator"}),
        )
        .await
        .unwrap();
        let ChangeProjectMembershipRoleOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    // -- decide_delete_project_membership ----------------------------------

    #[tokio::test]
    async fn a_sysadmin_deletes_a_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            true,
            "admin",
            None,
            None,
            "proj-a",
            &format!("u:{alice}"),
        )
        .await
        .unwrap();
        assert!(matches!(
            outcome,
            DeleteProjectMembershipOutcome::Deleted(_)
        ));
        assert!(
            identity::project_membership_role(&db, "proj-a", Some(&alice), None)
                .await
                .unwrap()
                .is_none()
        );
    }

    /// R7-F1 sibling for `decide_delete_project_membership`: same
    /// uniform 404 for a non-member on an existing-hidden project, and
    /// the target membership must survive the rejected delete.
    #[tokio::test]
    async fn delete_a_non_member_gets_the_uniform_404_for_an_existing_hidden_project() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-hidden");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let victim = seed_user(&db, "victim").await;
        identity::grant_project_membership(&db, "proj-hidden", Some(&victim), None, "operator")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some("bob"),
            None,
            "proj-hidden",
            &format!("u:{victim}"),
        )
        .await
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        assert!(
            identity::project_membership_role(&db, "proj-hidden", Some(&victim), None)
                .await
                .unwrap()
                .is_some(),
            "the unauthorized delete must be a no-op"
        );
    }

    #[tokio::test]
    async fn rejects_deleting_an_unknown_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let outcome = decide_delete_project_membership(
            &c, &db, &registry, true, "admin", None, None, "proj-a", "u:nobody",
        )
        .await
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[tokio::test]
    async fn a_delegate_with_no_role_at_all_gets_the_uniform_404_not_403() {
        // test_sec_r12_revoke_amplification.py's
        // `test_delegate_cannot_revoke_project_membership_with_no_role`:
        // R7-F1 closes the project-existence oracle -- a delegate who
        // simply isn't a member of a REAL project gets the SAME 404 a
        // nonexistent project would, not a 403.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let victim = seed_user(&db, "victim").await;
        identity::grant_project_membership(&db, "proj-a", Some(&victim), None, "operator")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            false,
            "mallory",
            Some("mallory"),
            None,
            "proj-a",
            &format!("u:{victim}"),
        )
        .await
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 404);
        assert!(
            identity::project_membership_role(&db, "proj-a", Some(&victim), None)
                .await
                .unwrap()
                .is_some(),
            "the revoke must have been blocked"
        );
    }

    #[tokio::test]
    async fn an_operator_delegate_can_revoke_a_viewers_membership() {
        // test_sec_r12_revoke_amplification.py's
        // `test_operator_delegate_can_revoke_viewer_membership`: the
        // guard only blocks revoking authority BEYOND the caller's own
        // -- a role at or below their own must still succeed.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let bob = seed_user(&db, "bob").await;
        identity::grant_project_membership(&db, "proj-a", Some(&bob), None, "operator")
            .await
            .unwrap();
        let victim = seed_user(&db, "victim").await;
        identity::grant_project_membership(&db, "proj-a", Some(&victim), None, "viewer")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("operator"),
            "proj-a",
            &format!("u:{victim}"),
        )
        .await
        .unwrap();
        assert!(matches!(
            outcome,
            DeleteProjectMembershipOutcome::Deleted(_)
        ));
    }

    #[tokio::test]
    async fn a_sysadmin_can_revoke_any_project_membership() {
        // test_sec_r12_revoke_amplification.py's
        // `test_sysadmin_can_revoke_project_membership`.
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let victim = seed_user(&db, "victim").await;
        identity::grant_project_membership(&db, "proj-a", Some(&victim), None, "operator")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            true,
            "root",
            None,
            None,
            "proj-a",
            &format!("u:{victim}"),
        )
        .await
        .unwrap();
        assert!(matches!(
            outcome,
            DeleteProjectMembershipOutcome::Deleted(_)
        ));
    }

    #[tokio::test]
    async fn a_viewer_cannot_revoke_an_operators_membership() {
        let dir = TempDir::new().unwrap();
        let registry = registry_with(&dir, "proj-a");
        let (_db_dir, c, db) = conn_with_sea_orm().await;
        let bob = seed_user(&db, "bob").await;
        identity::grant_project_membership(&db, "proj-a", Some(&bob), None, "viewer")
            .await
            .unwrap();
        let alice = seed_user(&db, "alice").await;
        identity::grant_project_membership(&db, "proj-a", Some(&alice), None, "operator")
            .await
            .unwrap();
        let outcome = decide_delete_project_membership(
            &c,
            &db,
            &registry,
            false,
            "bob",
            Some(&bob),
            Some("viewer"),
            "proj-a",
            &format!("u:{alice}"),
        )
        .await
        .unwrap();
        let DeleteProjectMembershipOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        assert!(
            identity::project_membership_role(&db, "proj-a", Some(&alice), None)
                .await
                .unwrap()
                .is_some()
        );
    }
}
