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

/// Port of test_sec_r6f2_stale_principal_toctou.py /
/// test_sec_r9f4_session_validity_toctou.py, one layer down: those
/// Python tests race a REAL slow-drip body read against a concurrent
/// revocation/logout, because aiohttp's `req.read()` is a genuine
/// mid-handler await Python code controls. Axum's `Bytes` extractor
/// already resolves the body BEFORE any of these handler functions
/// runs at all (see `perm_gates.rs`'s own module doc for why that
/// yield point moved outside this crate's code) -- so pacing it from
/// inside a test here can't reach the real race window either.
///
/// What's still fully real and testable at this layer: every handler
/// below is called with a `GateIdentity` built from live DB state
/// BEFORE a capability/session is revoked (the exact snapshot
/// `require_operator_session_middleware` caches once at entry, before
/// ANY yield point) -- proving the entry-time gate
/// (`require_*_capability`) would still ADMIT on that stale identity,
/// while `perm_gates::read_body_and_revalidate`'s later, FRESH re-read
/// of the SAME live DB correctly denies. That is the entire property
/// R6-F2/R9-F4 pin: a destructive write must never complete off an
/// entry-time snapshot that's gone stale by the time the handler's
/// own revalidation runs -- independent of what mechanism separates
/// the two reads in time.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
    use crate::login;
    use crate::orchestrator::ensure::EnsureConfig;
    use crate::project_registry::ProjectRegistry;
    use crate::rate_limit::RateLimitConfig;
    use crate::state::RouterStateConfig;
    use conexus_auth::capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_router_schema;
    use conexus_db::{group_capability_repository, group_membership_repository};
    use rusqlite::OptionalExtension;

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    async fn real_state() -> (tempfile::TempDir, Arc<RouterState>) {
        let dir = tempfile::TempDir::new().unwrap();
        let conn = Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let sea_orm_db = sea_orm::Database::connect("sqlite::memory:").await.unwrap();
        let state = Arc::new(RouterState::new(
            conn,
            sea_orm_db,
            registry,
            RateLimitConfig::resolve(|_| None),
            EnsureConfig::from_env(|_| None),
            RouterStateConfig {
                sock_dir: dir.path().join("sockets"),
                dashboard_dir: None,
                external_url: None,
                idle_sec: 14400,
                asset_prefix: None,
                single_tenant_name: None,
                single_tenant_workspace: None,
                max_streams_per_agent: 4,
                max_streams_global: 64,
                default_workspace_parent: dir.path().join("projects"),
                token_dir: None,
            },
        ));
        (dir, state)
    }

    /// A LIVE-derived `GateIdentity` -- exactly the resolution
    /// `evaluate_session_gate` performs once at entry, built here
    /// on-demand so a test can snapshot it BEFORE mutating the DB and
    /// still call a handler with that now-stale `Extension`.
    fn identity_for(conn: &Connection, user_id: &str) -> GateIdentity {
        let user = identity::get_user_by_id(conn, user_id).unwrap().unwrap();
        let groups = group_membership_repository::resolve_user_groups(conn, user_id).ok();
        let is_sysadmin =
            group_membership_repository::resolve_user_is_sysadmin(conn, user_id, groups.as_ref())
                .unwrap_or(false);
        let capabilities = resolve_capabilities(
            Some(conn),
            ResolveCapabilitiesInput {
                sysadmin: is_sysadmin,
                kind: PrincipalKind::OperatorSession,
                agent_role: None,
                user_id: Some(user_id),
                project_role: None,
                groups: groups.as_ref(),
            },
        )
        .unwrap();
        let principal = Principal {
            kind: PrincipalKind::OperatorSession,
            user_id: Some(user_id.to_string()),
            agent_id: None,
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            capabilities,
        };
        GateIdentity {
            user,
            is_sysadmin,
            project: None,
            project_role: None,
            principal,
        }
    }

    /// Seeds a genuinely NON-sysadmin user carrying `caps` via a fresh
    /// delegated group -- mirrors every Python pentest fixture's
    /// "dev/alice carries a capability via GROUP grant, not raw
    /// sysadmin" shape (R6-F2/R9-F4's whole attack surface is a
    /// caller who is NOT sysadmin, so revoking their one delegated
    /// capability -- or their session -- is the entire privilege).
    /// Returns `(user_id, group_id, identity-built-before-any-revocation)`.
    async fn seed_delegate(
        state: &RouterState,
        username: &str,
        caps: &[&str],
    ) -> (String, String, GateIdentity) {
        let mut conn = state.conn.lock().await;
        let is_empty: i64 = conn
            .query_row("SELECT COUNT(*) AS n FROM users", [], |r| r.get(0))
            .unwrap();
        if is_empty == 0 {
            identity::create_user(
                &mut conn,
                "__test_first_sysadmin",
                "ignoredsentinelpassword",
                None,
                false,
                true,
                &[],
                NOW,
            )
            .unwrap();
        }
        let uid = identity::create_user(
            &mut conn,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let group =
            group_membership_repository::create_group(&conn, &format!("g-{username}"), false, NOW)
                .unwrap();
        group_capability_repository::replace(&conn, &group.group_id, caps.iter().copied()).unwrap();
        group_membership_repository::add_group_member(
            &conn,
            &group.group_id,
            Some(&uid),
            None,
            NOW,
        )
        .unwrap();
        let identity = identity_for(&conn, &uid);
        (uid, group.group_id, identity)
    }

    async fn revoke_capability(state: &RouterState, group_id: &str) {
        let conn = state.conn.lock().await;
        group_capability_repository::replace(&conn, group_id, std::iter::empty()).unwrap();
    }

    fn json_body(v: serde_json::Value) -> Bytes {
        Bytes::from(serde_json::to_vec(&v).unwrap())
    }

    fn resp_status(resp: &Response) -> u16 {
        resp.status().as_u16()
    }

    async fn resp_json(resp: Response) -> serde_json::Value {
        let body = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        serde_json::from_slice(&body).unwrap()
    }

    // -- test_sec_admin_reads_gating.py: the 3 GET routes must be ------
    // -- gated on the SAME capability as their sibling mutations -------

    /// A non-sysadmin caller with NO capability grant at all (the
    /// "viewer" shape) must be denied 403 on every one of the three
    /// admin-namespace GET routes -- these routes were flagged (owner-
    /// authorized defensive review) as `gated(...)`-only (session
    /// auth, no capability check) in the pre-port Python source. This
    /// port's handlers already call `require_*_capability` as their
    /// own first line (confirmed by reading the source directly), but
    /// had zero test coverage proving it -- verified here end-to-end.
    #[tokio::test]
    async fn list_users_handler_denies_a_viewer_with_no_capability() {
        let (_dir, state) = real_state().await;
        let (_vera_id, _group_id, identity) = seed_delegate(&state, "vera", &[]).await;
        let resp = list_users_handler(State(state.clone()), Extension(identity)).await;
        assert_eq!(resp_status(&resp), 403);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "forbidden");
        assert!(body["message"]
            .as_str()
            .unwrap()
            .contains("system.users.manage"));
    }

    #[tokio::test]
    async fn list_groups_handler_denies_a_viewer_with_no_capability() {
        let (_dir, state) = real_state().await;
        let (_vera_id, _group_id, identity) = seed_delegate(&state, "vera", &[]).await;
        let resp = list_groups_handler(State(state.clone()), Extension(identity)).await;
        assert_eq!(resp_status(&resp), 403);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "forbidden");
        assert!(body["message"]
            .as_str()
            .unwrap()
            .contains("system.groups.manage"));
    }

    #[tokio::test]
    async fn list_project_memberships_handler_denies_a_viewer_with_no_capability() {
        let (_dir, state) = real_state().await;
        let (_vera_id, _group_id, identity) = seed_delegate(&state, "vera", &[]).await;
        state
            .registry
            .register("alpha", "/ws/alpha", "python", chrono::Utc::now())
            .unwrap();
        let resp = list_project_memberships_handler(
            State(state.clone()),
            Extension(identity),
            Path("alpha".to_string()),
        )
        .await;
        assert_eq!(resp_status(&resp), 403);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "forbidden");
        assert!(body["message"]
            .as_str()
            .unwrap()
            .contains("system.projects.manage"));
    }

    /// Regression: a delegate holding the SAME capability as the
    /// sibling mutation (Wave-9 group-delegation shape) is admitted --
    /// the new gate must not over-reject the legitimate delegated
    /// read path.
    #[tokio::test]
    async fn list_users_handler_admits_a_delegated_capability_holder() {
        let (_dir, state) = real_state().await;
        let (_alice_id, _group_id, identity) =
            seed_delegate(&state, "alice", &[Capability::SystemUsersManage.as_str()]).await;
        let resp = list_users_handler(State(state.clone()), Extension(identity)).await;
        assert_eq!(resp_status(&resp), 200);
    }

    #[tokio::test]
    async fn list_groups_handler_admits_a_delegated_capability_holder() {
        let (_dir, state) = real_state().await;
        let (_alice_id, _group_id, identity) =
            seed_delegate(&state, "alice", &[Capability::SystemGroupsManage.as_str()]).await;
        let resp = list_groups_handler(State(state.clone()), Extension(identity)).await;
        assert_eq!(resp_status(&resp), 200);
    }

    #[tokio::test]
    async fn list_project_memberships_handler_admits_a_sysadmin() {
        let (_dir, state) = real_state().await;
        state
            .registry
            .register("alpha", "/ws/alpha", "python", chrono::Utc::now())
            .unwrap();
        let identity = {
            let mut conn = state.conn.lock().await;
            let uid = identity::create_user(
                &mut conn,
                "root",
                "correct horse battery staple",
                None,
                false,
                true,
                &[],
                NOW,
            )
            .unwrap();
            identity_for(&conn, &uid)
        };
        let resp = list_project_memberships_handler(
            State(state.clone()),
            Extension(identity),
            Path("alpha".to_string()),
        )
        .await;
        assert_eq!(resp_status(&resp), 200);
    }

    // -- PF-R20-1 (test_sec_r20_json_recursion_depth.py, Site 2) -------

    /// `POST /api/router/users` -- the sibling site to `lifecycle_
    /// rest.rs::create_project_handler`'s own R20 test. Both route
    /// through the SAME `perm_gates::read_body_and_revalidate` ->
    /// `json_sanitize::decode_untrusted_body` chokepoint, whose
    /// pre-parse nesting-depth scan already rejects a body this deep
    /// with a clean 400 before `serde_json` ever parses it -- verified
    /// here end-to-end through the real handler.
    #[tokio::test]
    async fn create_user_handler_denies_deep_json_with_a_clean_400_not_a_crash() {
        let (_dir, state) = real_state().await;
        let (_dev_id, _group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;

        const DEEP_DEPTH: usize = 10_000;
        let mut deep_body = "[".repeat(DEEP_DEPTH);
        deep_body.push_str(&"]".repeat(DEEP_DEPTH));

        let resp = create_user_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            Bytes::from(deep_body),
        )
        .await;
        assert_eq!(resp_status(&resp), 400);
    }

    // -- R15-F2 (test_sec_r15_f2_admin_users_sanitizer.py) -------------

    /// Hidden-Unicode spoofing characters (ZWSP, RTLO) in `email` must
    /// be stripped before the row is ever written, and must not be
    /// echoed back verbatim by a subsequent read -- exactly the R13-F2/
    /// R14-F3 character classes stripped everywhere else. Already
    /// guaranteed structurally in Rust (`create_user_handler` decodes
    /// its body via `perm_gates::read_body_and_revalidate` ->
    /// `json_sanitize::decode_untrusted_body`, the ONE chokepoint every
    /// mutating `/api` body decodes through -- there was never a
    /// bespoke, un-sanitized `_json_body` equivalent to bypass in this
    /// port), but the file's own finding was flagged as untested at
    /// the route level even though the sanitizer itself and
    /// `identity::create_user_row`'s independent CLI/SSO-path
    /// sanitization each have their own coverage -- verified here
    /// end-to-end through the real handler.
    #[tokio::test]
    async fn create_user_handler_strips_hidden_unicode_from_email() {
        let (_dir, state) = real_state().await;
        let (_dev_id, _group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;

        let zwsp = "\u{200b}";
        let rtlo = "\u{202e}";
        let spoof_email = format!("abc{zwsp}{rtlo}def@example.com");
        let body = json_body(serde_json::json!({
            "username": "spoofuser",
            "password": "longenoughpassword",
            "email": spoof_email,
        }));
        let resp = create_user_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 201);
        let body = resp_json(resp).await;
        let created_email = body["user"]["email"].as_str().unwrap();
        assert!(!created_email.contains(zwsp), "{created_email:?}");
        assert!(!created_email.contains(rtlo), "{created_email:?}");
        assert_eq!(created_email, "abcdef@example.com");
    }

    /// Regression: a real internationalised (non-Latin) email must
    /// round-trip unchanged -- the sanitizer must not over-strip
    /// legitimate content.
    #[tokio::test]
    async fn create_user_handler_preserves_real_non_latin_email() {
        let (_dir, state) = real_state().await;
        let (_dev_id, _group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;

        let real_email = "用户@例え.jp";
        let body = json_body(serde_json::json!({
            "username": "intluser",
            "password": "longenoughpassword",
            "email": real_email,
        }));
        let resp = create_user_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 201);
        let body = resp_json(resp).await;
        assert_eq!(body["user"]["email"], real_email);
    }

    // -- R6-F2: capability revoked between entry gate and revalidation --

    #[tokio::test]
    async fn create_user_handler_denies_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (_dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({
            "username": "raced-in-user",
            "password": "somepasswordvalue",
        }));
        let resp = create_user_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        assert!(
            identity::get_user_by_username(&conn, "raced-in-user")
                .unwrap()
                .is_none(),
            "user must NOT have been created off the stale, pre-revocation grant"
        );
    }

    /// The LIVE-EXPLOITED repro shape (test A): a caller whose OWN
    /// sysadmin-granting privilege is revoked mid-flight must not be
    /// able to mint a NEW sysadmin off the stale snapshot --
    /// `fresh_is_sysadmin(&principal)` (this file's own documented
    /// TOCTOU fix) must see the post-revocation state, not
    /// `identity.is_sysadmin`.
    #[tokio::test]
    async fn edit_user_handler_denies_a_sysadmin_grant_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;
        let (victim_id, _victim_group, _victim_identity) =
            seed_delegate(&state, "victim", &[]).await;
        revoke_capability(&state, &group_id).await;
        let _ = dev_id;

        let body = json_body(serde_json::json!({"is_sysadmin": true}));
        let resp = edit_user_handler(
            State(state.clone()),
            Extension(identity),
            Path(victim_id.clone()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let victim = identity::get_user_by_id(&conn, &victim_id)
            .unwrap()
            .unwrap();
        assert!(
            !victim.is_sysadmin,
            "victim must NOT have been promoted off dev's stale, pre-revocation grant"
        );
    }

    /// R9-F4: a session invalidated (logged out) between entry-gate
    /// resolution and this handler's own revalidation must be denied
    /// too -- re-deriving capability/group membership fresh isn't
    /// enough on its own if the underlying SESSION is already dead.
    #[tokio::test]
    async fn edit_user_handler_denies_a_session_logged_out_before_the_call() {
        let (_dir, state) = real_state().await;
        let (dev_id, _group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;
        let (victim_id, _victim_group, _victim_identity) =
            seed_delegate(&state, "victim", &[]).await;

        let sid = {
            let conn = state.conn.lock().await;
            identity::create_session(&conn, &dev_id, NOW, "2026-02-01T00:00:00.000+00:00").unwrap()
        };
        let cookie = format!("{}={}", login::SESSION_COOKIE_NAME, sid);
        let mut headers = HeaderMap::new();
        headers.insert("cookie", cookie.parse().unwrap());

        // Simulate the concurrent logout landing before this paused
        // request resumes -- the session row is genuinely gone by the
        // time the handler's own revalidation reads it.
        {
            let conn = state.conn.lock().await;
            identity::delete_session(&conn, &sid).unwrap();
        }

        let body = json_body(serde_json::json!({"email": "raced-in@example.test"}));
        let resp = edit_user_handler(
            State(state.clone()),
            Extension(identity),
            Path(victim_id.clone()),
            headers,
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let victim = identity::get_user_by_id(&conn, &victim_id)
            .unwrap()
            .unwrap();
        assert_ne!(
            victim.email.as_deref(),
            Some("raced-in@example.test"),
            "edit must NOT have landed using an already-logged-out session"
        );
    }

    #[tokio::test]
    async fn create_group_handler_denies_a_sysadmin_flagged_group_off_a_capability_revoked_before_the_call(
    ) {
        let (_dir, state) = real_state().await;
        let (_dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemGroupsManage.as_str()]).await;
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({
            "name": "raced-sysadmin-group",
            "is_sysadmin": true,
        }));
        let resp = create_group_handler(
            State(state.clone()),
            Extension(identity),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let row: Option<i64> = conn
            .query_row(
                "SELECT 1 FROM groups WHERE name = ?1",
                ["raced-sysadmin-group"],
                |r| r.get(0),
            )
            .optional()
            .unwrap();
        assert!(
            row.is_none(),
            "sysadmin-flagged group must NOT have been created off the stale grant"
        );
    }

    #[tokio::test]
    async fn edit_group_handler_denies_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (_dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemGroupsManage.as_str()]).await;
        let target_group_id = {
            let conn = state.conn.lock().await;
            group_membership_repository::create_group(&conn, "target-group", false, NOW)
                .unwrap()
                .group_id
        };
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({"name": "renamed-target-group"}));
        let resp = edit_group_handler(
            State(state.clone()),
            Extension(identity),
            Path(target_group_id.clone()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let row: Option<i64> = conn
            .query_row(
                "SELECT 1 FROM groups WHERE group_id = ?1 AND name = 'target-group'",
                [&target_group_id],
                |r| r.get(0),
            )
            .optional()
            .unwrap();
        assert!(row.is_some(), "group must NOT have been renamed");
    }

    #[tokio::test]
    async fn add_group_member_handler_denies_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (_dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemGroupsManage.as_str()]).await;
        let (newbie_id, _newbie_group, _newbie_identity) =
            seed_delegate(&state, "newbie", &[]).await;
        let target_group_id = {
            let conn = state.conn.lock().await;
            group_membership_repository::create_group(&conn, "target-group-2", false, NOW)
                .unwrap()
                .group_id
        };
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({"user_id": newbie_id}));
        let resp = add_group_member_handler(
            State(state.clone()),
            Extension(identity),
            Path(target_group_id.clone()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let members = group_membership_repository::resolve_user_groups(&conn, &newbie_id).unwrap();
        assert!(
            !members.contains(&target_group_id),
            "newbie must NOT have been added to the group off the stale grant"
        );
    }

    #[tokio::test]
    async fn replace_group_capabilities_handler_denies_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (_dev_id, group_id, identity) = seed_delegate(
            &state,
            "dev",
            &[Capability::SystemGroupsCapabilitiesManage.as_str()],
        )
        .await;
        let target_group_id = {
            let conn = state.conn.lock().await;
            group_membership_repository::create_group(&conn, "target-group-3", false, NOW)
                .unwrap()
                .group_id
        };
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({
            "capabilities": [Capability::SystemUsersManage.as_str()],
        }));
        let resp = replace_group_capabilities_handler(
            State(state.clone()),
            Extension(identity),
            Path(target_group_id.clone()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let caps = group_capability_repository::fetch(&conn, &target_group_id).unwrap();
        assert!(
            caps.is_empty(),
            "target group's capabilities must NOT have been replaced off the stale grant"
        );
    }

    #[tokio::test]
    async fn add_project_membership_handler_denies_off_a_capability_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemProjectsManage.as_str()]).await;
        let (newbie_id, _newbie_group, _newbie_identity) =
            seed_delegate(&state, "newbie", &[]).await;
        state
            .registry
            .register("proj-a", "/ws/proj-a", "python", chrono::Utc::now())
            .unwrap();
        {
            let conn = state.conn.lock().await;
            identity::grant_project_membership(&conn, "proj-a", Some(&dev_id), None, "operator")
                .unwrap();
        }
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({"user_id": newbie_id, "role": "viewer"}));
        let resp = add_project_membership_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-a".to_string()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn, &newbie_id, "proj-a", None,
        )
        .unwrap();
        assert!(
            role.is_none(),
            "newbie must NOT have been granted project membership off the stale grant"
        );
    }

    /// R9-F3/R5-F1/R7-F1 (test_sec_r5_membership_self_escalation.py /
    /// test_sec_r7_membership_write_oracle.py): a delegate holding
    /// `system.projects.manage` via a group, with ZERO membership on
    /// the target project, must get the SAME uniform 404 `not_found` a
    /// nonexistent project produces -- never a 403, which would leak
    /// that the project genuinely exists (the project-existence oracle
    /// `project_gate::deny_cross_tenant_project_read` exists to
    /// close). Found-and-fixed bug (this PR): `perm_gates::revalidate`
    /// mapped `RevalidateOutcome::DeniedMembership` (Python's
    /// `_deny_cross_tenant_project_read`'s `role is None` branch, a
    /// 404) to a bespoke 403 "project membership revoked" instead --
    /// this is the FIRST call `add_project_membership_handler` makes
    /// (`read_body_and_revalidate`, before `decide_add_project_
    /// membership`'s own already-correct 404 ever gets a chance to
    /// run), so every non-racing request from a zero-membership
    /// delegate hit this wrong 403, not just the TOCTOU-race shape.
    #[tokio::test]
    async fn add_project_membership_handler_denies_a_zero_membership_delegate_with_uniform_404() {
        let (_dir, state) = real_state().await;
        let (alice_id, _group_id, identity) = seed_delegate(
            &state,
            "alice",
            &[Capability::SystemProjectsManage.as_str()],
        )
        .await;
        state
            .registry
            .register("proj-x", "/ws/proj-x", "python", chrono::Utc::now())
            .unwrap();
        // Deliberately NO project_membership row for alice on proj-x.

        let body = json_body(serde_json::json!({"user_id": alice_id, "role": "operator"}));
        let resp = add_project_membership_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-x".to_string()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 404);
        let body = resp_json(resp).await;
        assert_eq!(body["success"], false);
        assert_eq!(body["error"], "not_found");

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn, &alice_id, "proj-x", None,
        )
        .unwrap();
        assert!(
            role.is_none(),
            "alice must NOT have self-granted membership"
        );
    }

    /// Same finding, TOCTOU-race shape (the exact R9-F3 Python repro):
    /// alice has BOTH the capability AND a real `operator` membership
    /// at entry time, but her membership is stripped (capability left
    /// untouched) before this handler's own revalidation runs -- the
    /// SAME uniform 404 must fire, not the old 403.
    #[tokio::test]
    async fn add_project_membership_handler_denies_off_a_membership_revoked_before_the_call() {
        let (_dir, state) = real_state().await;
        let (alice_id, _group_id, identity) = seed_delegate(
            &state,
            "alice",
            &[Capability::SystemProjectsManage.as_str()],
        )
        .await;
        let (mallory_id, _mallory_group, _mallory_identity) =
            seed_delegate(&state, "mallory", &[]).await;
        state
            .registry
            .register("proj-y", "/ws/proj-y", "python", chrono::Utc::now())
            .unwrap();
        {
            let conn = state.conn.lock().await;
            identity::grant_project_membership(&conn, "proj-y", Some(&alice_id), None, "operator")
                .unwrap();
            // Strip it again before the handler ever runs -- capability
            // is untouched, mirroring the "membership revoked mid-
            // flight" repro without needing a real concurrent task
            // (axum's `Bytes` extractor already resolves the body
            // before any handler code runs -- see this module's own
            // doc on why the real yield-point race isn't reproducible
            // here).
            identity::remove_project_membership(&conn, "proj-y", Some(&alice_id), None).unwrap();
        }

        let body = json_body(serde_json::json!({"user_id": mallory_id, "role": "operator"}));
        let resp = add_project_membership_handler(
            State(state.clone()),
            Extension(identity),
            Path("proj-y".to_string()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 404);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "not_found");

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn,
            &mallory_id,
            "proj-y",
            None,
        )
        .unwrap();
        assert!(
            role.is_none(),
            "mallory must NOT have been granted membership"
        );
    }

    #[tokio::test]
    async fn change_project_membership_role_handler_denies_off_a_capability_revoked_before_the_call(
    ) {
        let (_dir, state) = real_state().await;
        let (dev_id, group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemProjectsManage.as_str()]).await;
        let (target_id, _target_group, _target_identity) =
            seed_delegate(&state, "target", &[]).await;
        state
            .registry
            .register("proj-b", "/ws/proj-b", "python", chrono::Utc::now())
            .unwrap();
        {
            let conn = state.conn.lock().await;
            identity::grant_project_membership(&conn, "proj-b", Some(&dev_id), None, "operator")
                .unwrap();
            identity::grant_project_membership(&conn, "proj-b", Some(&target_id), None, "viewer")
                .unwrap();
        }
        revoke_capability(&state, &group_id).await;

        let body = json_body(serde_json::json!({"role": "operator"}));
        let resp = change_project_membership_role_handler(
            State(state.clone()),
            Extension(identity),
            Path(("proj-b".to_string(), format!("u:{target_id}"))),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 403);

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn, &target_id, "proj-b", None,
        )
        .unwrap();
        assert_eq!(
            role.as_deref(),
            Some("viewer"),
            "target's role must NOT have been changed off the stale grant"
        );
    }

    /// R9-F3 (test_sec_r9f3_membership_grant_toctou.py "Test B"),
    /// `change_project_membership_role_handler`'s sibling of the
    /// `add_project_membership_handler` fix above: a delegate with
    /// capability but ZERO membership on the target project must get
    /// the uniform 404, never the old 403.
    #[tokio::test]
    async fn change_project_membership_role_handler_denies_a_zero_membership_delegate_with_uniform_404(
    ) {
        let (_dir, state) = real_state().await;
        let (_alice_id, _group_id, identity) = seed_delegate(
            &state,
            "alice",
            &[Capability::SystemProjectsManage.as_str()],
        )
        .await;
        let (target_id, _target_group, _target_identity) =
            seed_delegate(&state, "target", &[]).await;
        state
            .registry
            .register("proj-z", "/ws/proj-z", "python", chrono::Utc::now())
            .unwrap();
        {
            let conn = state.conn.lock().await;
            identity::grant_project_membership(&conn, "proj-z", Some(&target_id), None, "viewer")
                .unwrap();
        }
        // Deliberately NO project_membership row for alice on proj-z.

        let body = json_body(serde_json::json!({"role": "operator"}));
        let resp = change_project_membership_role_handler(
            State(state.clone()),
            Extension(identity),
            Path(("proj-z".to_string(), format!("u:{target_id}"))),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 404);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "not_found");

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn, &target_id, "proj-z", None,
        )
        .unwrap();
        assert_eq!(
            role.as_deref(),
            Some("viewer"),
            "target's role must NOT have been changed by a non-member delegate"
        );
    }

    /// R7-F1 sibling for `delete_project_membership_handler`: no
    /// prior route-level test existed for this handler at all. Unlike
    /// add/change it has no body-read yield point (confirmed in its
    /// own doc comment), so it never hit the `read_body_and_revalidate`
    /// bug the other two did -- `decide_delete_project_membership`'s
    /// own `deny_cross_tenant_project_read` call was already correct.
    /// Verified here end-to-end through the real handler for parity
    /// with its two siblings' new coverage.
    #[tokio::test]
    async fn delete_project_membership_handler_denies_a_zero_membership_delegate_with_uniform_404()
    {
        let (_dir, state) = real_state().await;
        let (_alice_id, _group_id, identity) = seed_delegate(
            &state,
            "alice",
            &[Capability::SystemProjectsManage.as_str()],
        )
        .await;
        let (target_id, _target_group, _target_identity) =
            seed_delegate(&state, "target", &[]).await;
        state
            .registry
            .register("proj-w", "/ws/proj-w", "python", chrono::Utc::now())
            .unwrap();
        {
            let conn = state.conn.lock().await;
            identity::grant_project_membership(&conn, "proj-w", Some(&target_id), None, "viewer")
                .unwrap();
        }
        // Deliberately NO project_membership row for alice on proj-w.

        let resp = delete_project_membership_handler(
            State(state.clone()),
            Extension(identity),
            Path(("proj-w".to_string(), format!("u:{target_id}"))),
        )
        .await;
        assert_eq!(resp_status(&resp), 404);
        let body = resp_json(resp).await;
        assert_eq!(body["error"], "not_found");

        let conn = state.conn.lock().await;
        let role = group_membership_repository::resolve_user_project_role(
            &conn, &target_id, "proj-w", None,
        )
        .unwrap();
        assert_eq!(
            role.as_deref(),
            Some("viewer"),
            "target's membership must NOT have been removed by a non-member delegate"
        );
    }

    // -- happy-path regression: a non-racing delegate still succeeds --

    #[tokio::test]
    async fn non_racing_edit_user_still_succeeds() {
        let (_dir, state) = real_state().await;
        let (_dev_id, _group_id, identity) =
            seed_delegate(&state, "dev", &[Capability::SystemUsersManage.as_str()]).await;
        let (victim_id, _victim_group, _victim_identity) =
            seed_delegate(&state, "victim", &[]).await;

        let body = json_body(serde_json::json!({"email": "still-valid@example.test"}));
        let resp = edit_user_handler(
            State(state.clone()),
            Extension(identity),
            Path(victim_id.clone()),
            HeaderMap::new(),
            body,
        )
        .await;
        assert_eq!(resp_status(&resp), 200, "{:?}", resp.into_body());

        let conn = state.conn.lock().await;
        let victim = identity::get_user_by_id(&conn, &victim_id)
            .unwrap()
            .unwrap();
        assert_eq!(victim.email.as_deref(), Some("still-valid@example.test"));
    }
}
