//! Real axum handlers for `admin_users_api.py`'s users/groups REST
//! surface. Phase E2, `conexus-router-users-groups-rest` (PR23 step
//! 7 of the 10-PR app-wiring breakdown). Pure wiring over
//! already-built, already-tested decision functions
//! (`admin_users_users`/`admin_groups`/`admin_group_capabilities`/
//! `admin_group_members`/`admin_project_memberships`) -- this module
//! adds no new decision logic of its own, matching `lifecycle_rest.rs`'s
//! own precedent for the sibling `admin_api.py` surface.
//!
//! **Two-tier gate shape, confirmed against the real Python source
//! for EVERY route in this file, not assumed uniform**: each of the
//! 5 route groups (users/groups/group-members/group-capabilities/
//! project-memberships) is wrapped by its OWN `require_capability`
//! decorator at registration time (`system.users.manage`/
//! `system.groups.manage`/`system.groups.capabilities.manage`/
//! `system.projects.manage`) -- this is the FIRST, entry-time check,
//! mirrored here by calling [`project_gate::require_capability`] as
//! each handler's own first line. A route whose Python handler ALSO
//! has a body-read (`POST`/`PATCH`) additionally re-checks via
//! `read_body_and_revalidate` INSIDE the handler body, after that
//! yield point -- the same two-layer, not-redundant pattern
//! `lifecycle_rest.rs::create_project_handler` already established.
//! A route with no body at all (every `GET`/most `DELETE`s here) has
//! only the entry-time check, since there is no in-handler yield
//! point to re-validate around.

use std::sync::Arc;

use axum::extract::{Extension, Path, State};
use axum::http::HeaderMap;
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use chrono::Utc;
use conexus_core::capability::{Capabilities, Capability};
use conexus_core::principal::Principal;
use conexus_db::group_membership_repository;
use rusqlite::Connection;

use crate::admin_group_capabilities::{
    self, ListGroupCapabilitiesOutcome, ReplaceGroupCapabilitiesOutcome,
};
use crate::admin_group_members::{self, AddGroupMemberOutcome, RemoveGroupMemberOutcome};
use crate::admin_groups::{self, CreateGroupOutcome, DeleteGroupOutcome, EditGroupOutcome};
use crate::admin_project_memberships::{
    self, AddProjectMembershipOutcome, ChangeProjectMembershipRoleOutcome,
    DeleteProjectMembershipOutcome,
};
use crate::admin_users_gate;
use crate::admin_users_users::{self, CreateUserOutcome, DeleteUserOutcome, EditUserOutcome};
use crate::identity::IdentityError;
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::perm_gates::{self, RevalidationSpec};
use crate::project_gate::{self, GateError};
use crate::session_gate::GateIdentity;
use crate::state::RouterState;

fn internal_error(e: impl std::fmt::Display) -> HandlerResponse {
    HandlerResponse {
        status: 500,
        headers: Vec::new(),
        body: HandlerBody::Json(serde_json::json!({
            "success": false,
            "error": "internal",
            "message": e.to_string(),
        })),
    }
}

impl From<IdentityError> for HandlerResponse {
    fn from(e: IdentityError) -> Self {
        internal_error(e)
    }
}

impl From<rusqlite::Error> for HandlerResponse {
    fn from(e: rusqlite::Error) -> Self {
        internal_error(e)
    }
}

fn cookie_header(headers: &HeaderMap) -> Option<&str> {
    headers.get("cookie").and_then(|v| v.to_str().ok())
}

/// Port of `_caller_is_sysadmin(req)`'s post-revalidation read.
///
/// **Found-and-fixed real TOCTOU gap (this PR)**: every mutating
/// handler that reads a body ALSO re-validates via
/// `perm_gates::read_body_and_revalidate`, which returns a FRESH
/// `Principal` specifically so the caller's sysadmin-grant guard sees
/// post-yield state (Python's own `_caller_is_sysadmin(req)` reads
/// `req['principal']`, which `read_body_and_revalidate`'s real
/// implementation mutates in place -- `perm_gates.py:185`). The
/// original `create_user_handler`/`edit_user_handler`/
/// `create_group_handler`/`edit_group_handler` wiring discarded that
/// returned principal (`let (parsed, _principal) = ...`) and used the
/// STALE `identity.is_sysadmin` captured at session-gate time instead
/// -- reopening exactly the TOCTOU window `read_body_and_revalidate`
/// exists to close: a caller whose sysadmin status was revoked
/// between session-gate resolution and this handler's body-read
/// could still pass the "granting sysadmin is sysadmin-only"
/// self-escalation guard. Fixed by deriving `is_sysadmin` from the
/// returned `Principal` instead of the stale `GateIdentity`.
fn fresh_is_sysadmin(principal: &Principal) -> bool {
    matches!(principal.capabilities, Capabilities::Sysadmin)
}

fn require_users_capability(
    state: &RouterState,
    identity: &GateIdentity,
) -> Result<(), HandlerResponse> {
    project_gate::require_capability(
        identity,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        Capability::SystemUsersManage,
    )
}

/// Port of `list_users_handler`. No body, so only the entry-time
/// capability check applies -- no in-handler re-check yield point.
pub async fn list_users_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    if let Err(resp) = require_users_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    match admin_users_users::list_users_response(&conn) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `create_user_handler`.
pub async fn create_user_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_users_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemUsersManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_users_users::decide_create_user(
        &conn,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        &parsed_value,
        &now_str,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        CreateUserOutcome::Created(row) => admin_users_gate::success_envelope(
            serde_json::json!({"user": admin_users_users::user_public_json(&row)}),
            201,
        )
        .into_response(),
        CreateUserOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `edit_user_handler`.
pub async fn edit_user_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(user_id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_users_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemUsersManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_users_users::decide_edit_user(
        &mut conn,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        &user_id,
        &parsed_value,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        EditUserOutcome::Updated(row) => admin_users_gate::success_envelope(
            serde_json::json!({"user": admin_users_users::user_public_json(&row)}),
            200,
        )
        .into_response(),
        EditUserOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `delete_user_handler`. No body, so only the entry-time
/// capability check applies.
pub async fn delete_user_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(user_id): Path<String>,
) -> Response {
    if let Err(resp) = require_users_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let outcome = match admin_users_users::decide_delete_user(
        &mut conn,
        identity.is_sysadmin,
        &identity.user.username,
        &user_id,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        DeleteUserOutcome::Deleted(user_id) => {
            admin_users_gate::success_envelope(serde_json::json!({"deleted": user_id}), 200)
                .into_response()
        }
        DeleteUserOutcome::Rejected(resp) => resp.into_response(),
    }
}

fn require_groups_capability(
    state: &RouterState,
    identity: &GateIdentity,
) -> Result<(), HandlerResponse> {
    project_gate::require_capability(
        identity,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        Capability::SystemGroupsManage,
    )
}

/// Port of `list_groups_handler`. No body, entry-time check only.
pub async fn list_groups_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    match admin_groups::list_groups_response(&conn) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `create_group_handler`.
pub async fn create_group_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemGroupsManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_groups::decide_create_group(
        &conn,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        &parsed_value,
        &now_str,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        CreateGroupOutcome::Created(group) => admin_users_gate::success_envelope(
            serde_json::json!({"group": admin_groups::group_public_json(&group, 0)}),
            201,
        )
        .into_response(),
        CreateGroupOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `edit_group_handler`.
pub async fn edit_group_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemGroupsManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_groups::decide_edit_group(
        &mut conn,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        &group_id,
        &parsed_value,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        EditGroupOutcome::Updated(group, member_count) => admin_users_gate::success_envelope(
            serde_json::json!({"group": admin_groups::group_public_json(&group, member_count)}),
            200,
        )
        .into_response(),
        EditGroupOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `delete_group_handler`. No body, entry-time check only.
pub async fn delete_group_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let outcome = match admin_groups::decide_delete_group(
        &mut conn,
        identity.is_sysadmin,
        &identity.user.username,
        &group_id,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        DeleteGroupOutcome::Deleted(group_id) => {
            admin_users_gate::success_envelope(serde_json::json!({"deleted": group_id}), 200)
                .into_response()
        }
        DeleteGroupOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `list_group_members_handler`. No body, entry-time check
/// only. Same capability as groups CRUD (`system.groups.manage`) --
/// confirmed against the real Python registration, which reuses the
/// identical `groups_gate` variable for every group-member route too.
pub async fn list_group_members_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    match admin_group_members::list_group_members_response(&conn, &group_id) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `add_group_member_handler`.
pub async fn add_group_member_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemGroupsManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_group_members::decide_add_group_member(
        &mut conn,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        Some(&identity.user.user_id),
        Some(&principal),
        &group_id,
        &parsed_value,
        &now_str,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        AddGroupMemberOutcome::Added(member) => {
            admin_users_gate::success_envelope(serde_json::json!({"member": member}), 201)
                .into_response()
        }
        AddGroupMemberOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `remove_group_member_handler`. No body, so `caller_principal`
/// is the session-gate-resolved identity's own principal (there is no
/// later revalidation to make it "become" stale relative to, matching
/// Python's `_caller_is_sysadmin(req)` reading whatever `req['principal']`
/// the auth middleware set for this request).
pub async fn remove_group_member_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path((group_id, member_id)): Path<(String, String)>,
) -> Response {
    if let Err(resp) = require_groups_capability(&state, &identity) {
        return resp.into_response();
    }
    let mut conn = state.conn.lock().await;
    let outcome = match admin_group_members::decide_remove_group_member(
        &mut conn,
        identity.is_sysadmin,
        &identity.user.username,
        Some(&identity.user.user_id),
        Some(&identity.principal),
        &group_id,
        &member_id,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        RemoveGroupMemberOutcome::Removed(member_id) => {
            admin_users_gate::success_envelope(serde_json::json!({"removed": member_id}), 200)
                .into_response()
        }
        RemoveGroupMemberOutcome::Rejected(resp) => resp.into_response(),
    }
}

fn require_group_caps_capability(
    state: &RouterState,
    identity: &GateIdentity,
) -> Result<(), HandlerResponse> {
    project_gate::require_capability(
        identity,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        Capability::SystemGroupsCapabilitiesManage,
    )
}

/// Port of `list_group_capabilities_handler`. No body, entry-time
/// check only -- a genuinely separate capability
/// (`system.groups.capabilities.manage`) from groups/group-members'
/// `system.groups.manage`, confirmed against the real Python
/// registration.
pub async fn list_group_capabilities_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
) -> Response {
    if let Err(resp) = require_group_caps_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let outcome = match admin_group_capabilities::decide_list_group_capabilities(&conn, &group_id) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        ListGroupCapabilitiesOutcome::Found(caps) => {
            admin_users_gate::success_envelope(serde_json::json!({"capabilities": caps}), 200)
                .into_response()
        }
        ListGroupCapabilitiesOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `replace_group_capabilities_handler`. Has a body-read yield
/// point, so -- per the TOCTOU fix already applied to every sibling
/// mutating handler in this module -- `caller_is_sysadmin` is derived
/// from the FRESH, revalidated `Principal`
/// `perm_gates::read_body_and_revalidate` returns, never the stale
/// `identity.is_sysadmin` captured at session-gate time. This handler
/// was never wired before this PR, so there was no retroactive fix to
/// make here -- `decide_replace_group_capabilities` already took an
/// explicit `caller_principal` parameter from when it was first
/// built, matching the same pattern `admin_group_members.rs`'s
/// decision functions use.
pub async fn replace_group_capabilities_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(group_id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_group_caps_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemGroupsCapabilitiesManage,
        project: None,
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_group_capabilities::decide_replace_group_capabilities(
        &conn,
        &group_id,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        Some(&principal),
        &parsed_value,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        ReplaceGroupCapabilitiesOutcome::Replaced(caps) => {
            admin_users_gate::success_envelope(serde_json::json!({"capabilities": caps}), 200)
                .into_response()
        }
        ReplaceGroupCapabilitiesOutcome::Rejected(resp) => resp.into_response(),
    }
}

fn require_project_memberships_capability(
    state: &RouterState,
    identity: &GateIdentity,
) -> Result<(), HandlerResponse> {
    project_gate::require_capability(
        identity,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        Capability::SystemProjectsManage,
    )
}

/// Port of `_membership_grant_denied`'s own `caller_role` resolution:
/// a FRESH, explicit per-project lookup via
/// `group_membership_repository::resolve_user_project_role`, never a
/// value read off `Principal.project_role`. Confirmed by direct
/// comparison with the real Python source
/// (`store.resolve_user_project_role(caller_id, project_name)`,
/// called fresh inside `_membership_grant_denied` itself) --
/// `Principal.project_role` is populated by `session_gate`/
/// revalidation for whatever project scope THAT machinery resolved
/// (meaningless for these admin-namespace routes, which never thread
/// the `{name}` path segment through session-gate's own project
/// resolution), not a general-purpose "the caller's role on any
/// project I ask about" fact.
fn resolve_caller_role_on_project(
    conn: &Connection,
    caller_user_id: &str,
    project_name: &str,
) -> Result<Option<String>, GateError> {
    Ok(group_membership_repository::resolve_user_project_role(
        conn,
        caller_user_id,
        project_name,
        None,
    )?)
}

/// Port of `list_project_memberships_handler`. No body -- its own
/// two-step existence-then-role check (NOT
/// `deny_cross_tenant_project_read`, per `admin_project_memberships.rs`'s
/// own doc: even a sysadmin gets a real 404 for a genuinely
/// nonexistent project here, closing R3-F1's 200-roster/404 existence
/// differential) runs on the stale, session-gate-resolved identity --
/// there is no later yield point for it to become stale relative to.
pub async fn list_project_memberships_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
) -> Response {
    if let Err(resp) = require_project_memberships_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    match admin_project_memberships::decide_list_project_memberships(
        &conn,
        &state.registry,
        identity.is_sysadmin,
        Some(&identity.user.user_id),
        &name,
    ) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `add_project_membership_handler`. Has a body-read yield
/// point -- Python's own `read_body_and_revalidate` call for this
/// handler ALSO carries `project_name` (R9-F3), fusing the fresh
/// re-check over BOTH capability AND membership, not just capability
/// -- `caller_is_sysadmin` derives from the REVALIDATED `Principal`
/// this fusion returns, mirroring PR7c's TOCTOU fix for the
/// sysadmin-grant guard. `caller_role`, however, is a SEPARATE, fresh
/// per-project lookup (`resolve_caller_role_on_project`) rather than
/// anything read off the `Principal` -- see that helper's own doc for
/// why `Principal.project_role` was never the right source for this
/// admin-namespace route regardless of freshness (a real, found-and-
/// fixed bug in this PR's own first draft, caught by live-testing the
/// AZ-R12-1 role-rank guard against a genuine non-sysadmin delegate).
pub async fn add_project_membership_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_project_memberships_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: Some(perm_gates::RevalidationProject {
            project_name: &name,
            min_role: None,
        }),
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let caller_role = match resolve_caller_role_on_project(&conn, &identity.user.user_id, &name) {
        Ok(r) => r,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    let outcome = match admin_project_memberships::decide_add_project_membership(
        &conn,
        &state.registry,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        Some(&identity.user.user_id),
        caller_role.as_deref(),
        &name,
        &parsed_value,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        AddProjectMembershipOutcome::Added(member) => {
            admin_users_gate::success_envelope(serde_json::json!({"membership": member}), 201)
                .into_response()
        }
        AddProjectMembershipOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `change_project_membership_role_handler`. Same shape as
/// `add_project_membership_handler` above -- fresh-principal-derived
/// `caller_is_sysadmin` (its body-read yield point carries the
/// identical `project_name`-scoped revalidation), separately fresh
/// per-project `caller_role` via `resolve_caller_role_on_project`.
pub async fn change_project_membership_role_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path((name, membership_id)): Path<(String, String)>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(resp) = require_project_memberships_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let now_str = Utc::now().to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: Some(perm_gates::RevalidationProject {
            project_name: &name,
            min_role: None,
        }),
    };
    let (parsed, principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let caller_role = match resolve_caller_role_on_project(&conn, &identity.user.user_id, &name) {
        Ok(r) => r,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    let outcome = match admin_project_memberships::decide_change_project_membership_role(
        &conn,
        &state.registry,
        fresh_is_sysadmin(&principal),
        &identity.user.username,
        Some(&identity.user.user_id),
        caller_role.as_deref(),
        &name,
        &membership_id,
        &parsed_value,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        ChangeProjectMembershipRoleOutcome::Changed(member) => {
            admin_users_gate::success_envelope(serde_json::json!({"membership": member}), 200)
                .into_response()
        }
        ChangeProjectMembershipRoleOutcome::Rejected(resp) => resp.into_response(),
    }
}

/// Port of `delete_project_membership_handler`. No body-read at all
/// in the real Python source (confirmed directly, not assumed
/// symmetric with its siblings) -- so `identity.is_sysadmin` is used
/// AS-IS, correctly matching Python's own `req['principal']` never
/// being touched by a revalidation call for this one handler.
/// `caller_role`, however, is STILL a fresh per-project lookup, same
/// as every sibling -- see `resolve_caller_role_on_project`'s own doc
/// for why `Principal.project_role` was never the right source here
/// regardless of staleness.
pub async fn delete_project_membership_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    Path((name, membership_id)): Path<(String, String)>,
) -> Response {
    if let Err(resp) = require_project_memberships_capability(&state, &identity) {
        return resp.into_response();
    }
    let conn = state.conn.lock().await;
    let caller_role = match resolve_caller_role_on_project(&conn, &identity.user.user_id, &name) {
        Ok(r) => r,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    let outcome = match admin_project_memberships::decide_delete_project_membership(
        &conn,
        &state.registry,
        identity.is_sysadmin,
        &identity.user.username,
        Some(&identity.user.user_id),
        caller_role.as_deref(),
        &name,
        &membership_id,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };
    match outcome {
        DeleteProjectMembershipOutcome::Deleted(membership_id) => {
            admin_users_gate::success_envelope(serde_json::json!({"removed": membership_id}), 200)
                .into_response()
        }
        DeleteProjectMembershipOutcome::Rejected(resp) => resp.into_response(),
    }
}
