//! Real axum handlers for `admin_api.py`'s project-lifecycle REST
//! surface. Phase E2, `conexus-router-lifecycle-rest-basic` (PR23
//! step 6b of the 10-PR app-wiring breakdown, first slice of 6). Pure
//! wiring over already-built, already-tested decision functions
//! (`project_reads`/`project_gate`/`perm_gates`) -- this module adds
//! no new decision logic of its own.
//!
//! **`health_handler` takes no `Extension<GateIdentity>`** -- it's
//! wired into `state.rs`'s `extra_exact_paths`, so
//! `session_gate_layer` resolves it to `SessionGateOutcome::
//! PassThrough` and never inserts an identity extension at all
//! (matching Python's own `public_route` registration,
//! `admin_api.py:1723-1728`).
//!
//! **`create_project_handler` runs `project_gate::require_capability`
//! as its own first line, THEN `perm_gates::read_body_and_revalidate`
//! around the body-read** -- not a redundant double-check: this
//! mirrors Python's real two-decorator/one-body-fusion shape exactly
//! (`project_lifecycle_gate = require_capability(...)` wraps the
//! whole handler; `read_body_and_revalidate` re-checks AFTER the
//! body-read yield point, closing the TOCTOU window between entry and
//! that await -- gap 5 from this PR's own research, confirmed
//! harmless but real).

use std::sync::Arc;

use axum::extract::{Extension, State};
use axum::http::HeaderMap;
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use chrono::Utc;
use conexus_core::capability::Capability;

use crate::lifecycle;
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::perm_gates::{self, RevalidationSpec};
use crate::project_gate::{self, CreateProjectOutcome, GateError};
use crate::project_reads;
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

impl From<GateError> for HandlerResponse {
    fn from(e: GateError) -> Self {
        internal_error(e)
    }
}

fn cookie_header(headers: &HeaderMap) -> Option<&str> {
    headers.get("cookie").and_then(|v| v.to_str().ok())
}

/// Port of `health_handler`. Genuinely unauthenticated -- see this
/// module's own doc for why no `Extension<GateIdentity>` is taken.
pub async fn health_handler(State(state): State<Arc<RouterState>>) -> Response {
    project_reads::health_response(state.mcp_handler_config.single_tenant_name.as_deref())
        .into_response()
}

/// Port of `list_projects_handler` -- session-gated, but no
/// capability check at all (every authenticated caller can list the
/// projects visible to THEM; `visible_project_names` does the actual
/// scoping).
pub async fn list_projects_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
) -> Response {
    let conn = state.conn.lock().await;
    match project_reads::list_projects_response(
        &conn,
        &state.registry,
        state.mcp_handler_config.single_tenant_name.as_deref(),
        identity.is_sysadmin,
        Some(identity.user.user_id.as_str()),
    ) {
        Ok(resp) => resp.into_response(),
        Err(e) => HandlerResponse::from(e).into_response(),
    }
}

/// Port of `create_project_handler`.
pub async fn create_project_handler(
    State(state): State<Arc<RouterState>>,
    Extension(identity): Extension<GateIdentity>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let single_tenant_name = state.mcp_handler_config.single_tenant_name.as_deref();
    if let Err(resp) = project_gate::require_capability(
        &identity,
        single_tenant_name,
        Capability::SystemProjectsManage,
    ) {
        return resp.into_response();
    }

    let conn = state.conn.lock().await;
    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let spec = RevalidationSpec {
        stale_user_id: &identity.user.user_id,
        cookie_header: cookie_header(&headers),
        now: &now_str,
        cap: Capability::SystemProjectsManage,
        project: None,
    };
    let (parsed, _principal) = match perm_gates::read_body_and_revalidate(&conn, &body, &spec) {
        Ok(v) => v,
        Err(resp) => return resp.into_response(),
    };

    let outcome = match project_gate::decide_create_project(
        &conn,
        &state.registry,
        &state.default_workspace_parent,
        identity.is_sysadmin,
        Some(identity.user.user_id.as_str()),
        parsed.get("name"),
        now,
    ) {
        Ok(o) => o,
        Err(e) => return HandlerResponse::from(e).into_response(),
    };

    match outcome {
        CreateProjectOutcome::Created {
            name,
            workspace_label,
        } => lifecycle::success_envelope(
            serde_json::json!({"project": {"name": name, "workspace": workspace_label}}),
            201,
        )
        .into_response(),
        CreateProjectOutcome::Rejected(resp) => resp.into_response(),
    }
}
