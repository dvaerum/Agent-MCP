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
use conexus_core::capability::Capability;

use crate::admin_groups::{self, CreateGroupOutcome, DeleteGroupOutcome, EditGroupOutcome};
use crate::admin_users_gate;
use crate::admin_users_users::{self, CreateUserOutcome, DeleteUserOutcome, EditUserOutcome};
use crate::identity::IdentityError;
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::perm_gates::{self, RevalidationSpec};
use crate::project_gate;
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
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_users_users::decide_create_user(
        &conn,
        identity.is_sysadmin,
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
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_users_users::decide_edit_user(
        &mut conn,
        identity.is_sysadmin,
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
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_groups::decide_create_group(
        &conn,
        identity.is_sysadmin,
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
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };
    let parsed_value = serde_json::Value::Object(parsed);
    let outcome = match admin_groups::decide_edit_group(
        &mut conn,
        identity.is_sysadmin,
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
