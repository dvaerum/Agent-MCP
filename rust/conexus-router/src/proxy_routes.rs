//! Real axum route entry points for the MCP/API reverse-proxy paths
//! -- thin wrappers converting an axum `Request` into the plain
//! `mcp_handler::HandlerRequest` the already-ported, framework-
//! agnostic `backend_mcp_handler`/`backend_api_handler` take. Phase
//! E2, `conexus-router-mcp-api-proxy` (PR23 step 3 of the 10-PR
//! app-wiring breakdown).
//!
//! **Mounted OUTSIDE the session-gate middleware entirely** (see
//! `main.rs`'s own router-assembly comment) -- both handlers do their
//! OWN bearer/Accept-header admission logic before ever calling
//! `proxy_core::proxy_to_backend`, matching Python's real route
//! registration (`/agent-mcp/mcp/{name}` is itself in
//! `path_policy::UNAUTH_PREFIXES` -- the session gate already passes
//! it through unconditionally; `/agent-mcp/api/` is REDIRECT-exempt
//! but not unauth-exempt in Python's real path-policy tables, since a
//! cookie-authenticated dashboard browser call also flows through this
//! same route). [`api_proxy_handler`]/[`api_proxy_handler_no_rest`]
//! resolve that cookie-forwarding bridge (`crate::cookie_forwarding`)
//! themselves, in a SHORT `state.conn.lock().await` scope dropped
//! before `backend_api_handler` ever runs -- see
//! [`resolve_cookie_role_for_proxy`]'s own doc for why that split
//! exists. Still wrapped by security-headers/rate-limit/empty-users-
//! redirect (the empty-users-redirect is a documented no-op here
//! either way -- both prefixes are in
//! `path_policy::REDIRECT_EXEMPT_PREFIXES`).

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Method, Uri};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};

use conexus_auth::forwarding_header::ForwardedRole;

use crate::cookie_forwarding;
use crate::mcp_handler::{self, HandlerRequest};
use crate::orchestrator::resolve;
use crate::state::RouterState;

fn handler_request(
    method: Method,
    uri: &Uri,
    project_name: String,
    headers: HeaderMap,
    body: Bytes,
) -> HandlerRequest {
    HandlerRequest {
        method,
        project_name,
        path: uri.path().to_string(),
        query: uri.query().map(str::to_string),
        headers,
        body,
    }
}

/// `/agent-mcp/mcp/{name}` -- port of the `"*"` route Python registers
/// for `backend_mcp_handler`.
pub async fn mcp_proxy_handler(
    State(state): State<Arc<RouterState>>,
    Path(name): Path<String>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let req = handler_request(method, &uri, name, headers, body);
    mcp_handler::backend_mcp_handler(
        &state.runtime,
        &state.stream_caps,
        &state.registry,
        &state.sock_dir,
        &state.ensure_config,
        &state.mcp_handler_config,
        chrono::Utc::now(),
        req,
    )
    .await
    .into_response()
}

/// Resolve `req`'s cookie-authenticated project role, if any, for
/// [`api_proxy_handler`]/[`api_proxy_handler_no_rest`] to pass into
/// `backend_api_handler`'s `cookie_role` parameter.
///
/// **Why this lives here, not inside `backend_api_handler` itself**:
/// `RouterState.conn` is ONE shared `tokio::sync::Mutex` behind the
/// WHOLE router (every project's dashboard/admin traffic) -- borrowing
/// it for `backend_api_handler`'s entire async call would tie that
/// single lock to the full `ensure()`/UDS-proxy round-trip beneath it
/// (a cold-start `ensure()` can legitimately take several real
/// seconds). Every other `state.conn.lock().await` call site in this
/// crate scopes the guard tightly and drops it before any such
/// subsequent long-running async work (`lifecycle_rest.rs::
/// delete_project_handler`'s own `{}`-scoped-lock-then-proceed
/// precedent) -- this function is that same scope, factored out so
/// both proxy routes share it.
///
/// Returns `None` immediately, with NO lock taken at all, when a
/// bearer is already present (a stronger credential this crate never
/// downgrades away from) or when the URL segment doesn't resolve to a
/// real/aliased project (the real 404 is `backend_api_handler`'s own
/// job, via its own `resolve::resolve` call moments later).
async fn resolve_cookie_role_for_proxy(
    state: &RouterState,
    req: &HandlerRequest,
    now: DateTime<Utc>,
) -> Option<(String, ForwardedRole)> {
    if mcp_handler::extract_bearer(&req.headers).is_some() {
        return None;
    }
    let cookie_header = req
        .headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())?;
    let (real_name, _alias) = resolve::resolve(&state.registry, &req.project_name, now).ok()?;
    let conn = state.conn.lock().await;
    cookie_forwarding::resolve_cookie_project_role(&conn, Some(cookie_header), &real_name, now)
        .ok()
        .flatten()
}

/// `/agent-mcp/api/{name}/{*rest}` -- the common case, a real
/// sub-path under the project's API.
pub async fn api_proxy_handler(
    State(state): State<Arc<RouterState>>,
    Path((name, rest)): Path<(String, String)>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let req = handler_request(method, &uri, name, headers, body);
    let now = chrono::Utc::now();
    let cookie_role = resolve_cookie_role_for_proxy(&state, &req, now).await;
    mcp_handler::backend_api_handler(
        &state.runtime,
        &state.stream_caps,
        &state.registry,
        &state.sock_dir,
        &state.ensure_config,
        &state.mcp_handler_config,
        cookie_role,
        now,
        &rest,
        req,
    )
    .await
    .into_response()
}

/// `/agent-mcp/api/{name}` -- the no-trailing-segment case (Python's
/// `{rest:.*}` matches a zero-length suffix too; axum's `{*rest}`
/// catch-all requires a real segment, so this is a second route
/// mapping to the identical handler with `rest = ""`).
pub async fn api_proxy_handler_no_rest(
    State(state): State<Arc<RouterState>>,
    Path(name): Path<String>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let req = handler_request(method, &uri, name, headers, body);
    let now = chrono::Utc::now();
    let cookie_role = resolve_cookie_role_for_proxy(&state, &req, now).await;
    mcp_handler::backend_api_handler(
        &state.runtime,
        &state.stream_caps,
        &state.registry,
        &state.sock_dir,
        &state.ensure_config,
        &state.mcp_handler_config,
        cookie_role,
        now,
        "",
        req,
    )
    .await
    .into_response()
}
