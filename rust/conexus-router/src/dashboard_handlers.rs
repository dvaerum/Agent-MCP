//! Real axum handlers for the router's dashboard-serving surface --
//! wiring `dashboard_static.rs`'s decision functions into 4 read-only
//! routes: `index_handler` (content-negotiated JSON descriptor / 302
//! redirect), `dashboard_assets_handler`, `overview_dashboard_handler`,
//! `dashboard_handler` (per-project shell + SPA fallback). Phase E2,
//! `conexus-router-dashboard-static` (PR23 step 8, PR 2/3).
//!
//! The warm-start side effect (`_warm_backend`/`_schedule_backend_warm`)
//! stays deferred to PR 3/3, per `dashboard_static.rs`'s own module
//! doc -- `dashboard_handler` here serves the shell but never
//! triggers a backend spawn, matching this migration's own
//! "mechanical first" precedent for a tricky side effect.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, OriginalUri, Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::dashboard_static::{
    accept_prefers_html, resolve_dashboard_body, resolve_dashboard_candidate, service_descriptor,
};
use crate::middleware::{is_request_trusted, peer_info};
use crate::mount;
use crate::state::RouterState;

fn forwarded_prefix(headers: &HeaderMap) -> Option<String> {
    headers
        .get("x-forwarded-prefix")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

/// Port of every dashboard handler's own `mount.external_prefix(req) +
/// "/assets"` call -- computed fresh per request (never the CLI-
/// configured `--asset-prefix` static override, confirmed against the
/// real Python source: `dashboard_handler`/`overview_dashboard_handler`/
/// `dashboard_assets_handler` all pass this explicit `prefix=` kwarg,
/// never falling through to `_serve_dashboard_file`'s own
/// `ASSET_PREFIX` default).
fn resolve_asset_prefix(
    state: &RouterState,
    addr: SocketAddr,
    path: &str,
    headers: &HeaderMap,
) -> String {
    let peer = peer_info(addr);
    let is_trusted = is_request_trusted(state, &peer);
    let fp = forwarded_prefix(headers);
    format!(
        "{}/assets",
        mount::external_prefix(path, is_trusted, fp.as_deref())
    )
}

fn not_found() -> Response {
    StatusCode::NOT_FOUND.into_response()
}

/// Port of `web.HTTPMovedPermanently` -- a genuine 301, unlike axum's
/// own `Redirect::permanent()` helper (308, a real status-code
/// mismatch found and fixed here rather than reused: 308 preserves
/// the request method/body across the hop, which is Python's OWN
/// `HTTPPermanentRedirect` semantics, not `HTTPMovedPermanently`'s --
/// every one of these routes is GET-only so the practical difference
/// is nil for a real browser, but the wire-level status code should
/// still match the real source exactly).
pub(crate) fn moved_permanently(location: &str) -> Response {
    (
        StatusCode::MOVED_PERMANENTLY,
        [(header::LOCATION, location)],
    )
        .into_response()
}

/// Serves a resolved dashboard file candidate, or 404 if `dashboard_dir`
/// isn't configured (matches Python's own "no dashboard tree ->
/// nothing to resolve" behavior -- `_safe_dashboard_path` always
/// returns `None` against an empty/missing root).
fn serve_candidate(
    state: &RouterState,
    candidate: Option<std::path::PathBuf>,
    prefix: &str,
    cache_control: &'static str,
) -> Response {
    let Some(candidate) = candidate else {
        return not_found();
    };
    match resolve_dashboard_body(&state.asset_prefix_cache, &candidate, prefix) {
        Ok((body, content_type)) => (
            [
                (header::CONTENT_TYPE, content_type),
                (header::CACHE_CONTROL, cache_control),
            ],
            body,
        )
            .into_response(),
        Err(_) => not_found(),
    }
}

/// Port of `index_handler`. `GET /agent-mcp/` -- content-negotiated:
/// a browser (`Accept: text/html`) gets a 302 to the dashboard;
/// anything else gets the JSON service descriptor.
pub async fn index_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let accept = headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if accept_prefers_html(accept) {
        let single_tenant = state.mcp_handler_config.single_tenant_name.as_deref();
        let suffix = match single_tenant {
            Some(name) => {
                let encoded =
                    percent_encoding::utf8_percent_encode(name, crate::session_gate::QUOTE_SAFE)
                        .to_string();
                format!("/app/{encoded}/")
            }
            None => "/app/".to_string(),
        };
        let peer = peer_info(addr);
        let is_trusted = is_request_trusted(&state, &peer);
        let fp = forwarded_prefix(&headers);
        let location = mount::external_path(uri.path(), is_trusted, fp.as_deref(), &suffix);
        // Port of `web.HTTPFound` -- a genuine 302, not axum's built-in
        // `Redirect` helpers (303/307/308 only, no 302 constructor).
        return (StatusCode::FOUND, [(header::LOCATION, location)]).into_response();
    }
    let descriptor = service_descriptor(state.mcp_handler_config.single_tenant_name.as_deref());
    (
        [(header::CACHE_CONTROL, "no-store")],
        axum::Json(descriptor),
    )
        .into_response()
}

/// Port of `dashboard_assets_handler`. `GET /agent-mcp/assets/{*rest}`
/// -- Next.js content-hashes every chunk filename, so a hit is
/// immutable forever.
pub async fn dashboard_assets_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    Path(rest): Path<String>,
) -> Response {
    let Some(dashboard_dir) = state.dashboard_dir.as_deref() else {
        return not_found();
    };
    // Deliberately NOT `resolve_dashboard_candidate`'s SPA-fallback
    // chain -- confirmed against Python: `dashboard_assets_handler`
    // calls the plain `_safe_dashboard_path` + `is_file()` check
    // directly. An asset request with no matching file is a genuine
    // 404, never "serve the root page shell".
    let candidate =
        crate::dashboard_static::safe_dashboard_path(dashboard_dir, &rest).filter(|p| p.is_file());
    let prefix = resolve_asset_prefix(&state, addr, uri.path(), &headers);
    serve_candidate(
        &state,
        candidate,
        &prefix,
        "public, max-age=31536000, immutable",
    )
}

/// Port of `overview_dashboard_handler`. `GET /agent-mcp/app/` -- the
/// cross-project React overview shell.
pub async fn overview_dashboard_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
) -> Response {
    let Some(dashboard_dir) = state.dashboard_dir.as_deref() else {
        return not_found();
    };
    let candidate = crate::dashboard_static::safe_dashboard_path(dashboard_dir, "index.html")
        .filter(|p| p.is_file());
    let prefix = resolve_asset_prefix(&state, addr, uri.path(), &headers);
    serve_candidate(&state, candidate, &prefix, "no-store")
}

/// Shared body for both `dashboard_handler` route registrations below
/// -- axum has no "optional trailing wildcard" extractor, so the
/// bare-trailing-slash form (`rest = ""`) and the `{*rest}` form are
/// two distinct routes/handlers sharing this one implementation, both
/// ultimately porting the same Python `dashboard_handler`.
async fn dashboard_handler_impl(
    state: &Arc<RouterState>,
    addr: SocketAddr,
    uri_path: &str,
    headers: &HeaderMap,
    rest: &str,
) -> Response {
    let Some(dashboard_dir) = state.dashboard_dir.as_deref() else {
        return not_found();
    };
    let candidate = resolve_dashboard_candidate(dashboard_dir, rest);
    let prefix = resolve_asset_prefix(state, addr, uri_path, headers);
    serve_candidate(state, candidate, &prefix, "no-store")
}

/// Port of `dashboard_handler` for `GET /agent-mcp/app/{name}/` (the
/// bare per-project shell, `rest = ""`).
pub async fn dashboard_index_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    Path(_name): Path<String>,
) -> Response {
    dashboard_handler_impl(&state, addr, uri.path(), &headers, "").await
}

/// Port of `dashboard_handler` for `GET /agent-mcp/app/{name}/{*rest}`.
pub async fn dashboard_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    Path((_name, rest)): Path<(String, String)>,
) -> Response {
    dashboard_handler_impl(&state, addr, uri.path(), &headers, &rest).await
}
