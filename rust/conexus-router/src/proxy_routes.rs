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
//! cookie-authenticated dashboard browser call also flows through
//! this same route -- that cookie-forwarding composition is still
//! PR9's own documented, deliberate scope gap ("the bearer-
//! authenticated path only"), not something this step re-opens).
//! Still wrapped by security-headers/rate-limit/empty-users-redirect
//! (the empty-users-redirect is a documented no-op here either way --
//! both prefixes are in `path_policy::REDIRECT_EXEMPT_PREFIXES`).

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Method, Uri};
use axum::response::{IntoResponse, Response};

use crate::mcp_handler::{self, HandlerRequest};
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
    mcp_handler::backend_api_handler(
        &state.runtime,
        &state.stream_caps,
        &state.registry,
        &state.sock_dir,
        &state.ensure_config,
        &state.mcp_handler_config,
        chrono::Utc::now(),
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
    mcp_handler::backend_api_handler(
        &state.runtime,
        &state.stream_caps,
        &state.registry,
        &state.sock_dir,
        &state.ensure_config,
        &state.mcp_handler_config,
        chrono::Utc::now(),
        "",
        req,
    )
    .await
    .into_response()
}
