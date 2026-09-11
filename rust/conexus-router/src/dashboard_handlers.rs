//! Real axum handlers for the router's dashboard-serving surface --
//! wiring `dashboard_static.rs`'s decision functions into 4 read-only
//! routes: `index_handler` (content-negotiated JSON descriptor / 302
//! redirect), `dashboard_assets_handler`, `overview_dashboard_handler`,
//! `dashboard_handler` (per-project shell + SPA fallback). Phase E2,
//! `conexus-router-dashboard-static` (PR23 step 8).
//!
//! **PR 3/3 also lands here**: the `_warm_backend`/
//! `_schedule_backend_warm` side effect, gated on
//! `middleware::WarmAuthorized` (SC-R6-1 -- see that type's own doc
//! for the full authorization mapping this fixes a real, previously-
//! dropped `session_gate_layer` signal to carry).

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, Extension, OriginalUri, Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::dashboard_static::{
    accept_prefers_html, resolve_dashboard_body, resolve_dashboard_candidate, service_descriptor,
};
use crate::middleware::{is_request_trusted, peer_info, WarmAuthorized};
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

/// Port of `_warm_backend`: a best-effort, fire-and-forget lazy-spawn
/// of `name`'s backend. Swallows EVERY failure (unknown project, unit
/// error, spawn timeout) -- serving the static shell must NEVER
/// depend on the backend coming up; a real spawn error still surfaces
/// on the SUBSEQUENT `/api/<name>/...` XHR, which awaits `_ensure`
/// for real. Clears `warm_inflight` on exit via `RuntimeStore::forget`'s
/// own field-level reset semantics, reused here through
/// `with_runtime_mut` directly (a full `forget()` would also wrongly
/// clear `last_active`/the ensure lock this call may have just set).
async fn warm_backend(state: Arc<RouterState>, name: String) {
    let _ = crate::orchestrator::ensure::ensure(
        &state.runtime,
        &state.registry,
        &state.sock_dir,
        &name,
        "backend",
        &state.ensure_config,
    )
    .await;
    state
        .runtime
        .with_runtime_mut(&name, |rt| rt.warm_inflight = false);
}

/// The pure half of `_schedule_backend_warm`'s BL-R6-2a dedup: given
/// the row's CURRENT `warm_inflight`, decide whether THIS call should
/// be the one to spawn -- flips the flag to `true` and returns
/// `true` on a genuine transition, leaves it alone and returns
/// `false` when a warm-start is already pending. Extracted as its
/// own pure fn (mutate-and-decide, no I/O) so the dedup invariant is
/// unit-testable without `tokio::spawn`'s own scheduling timing.
fn should_spawn_warm(rt: &mut crate::orchestrator::runtime::ProjectRuntime) -> bool {
    if rt.warm_inflight {
        return false;
    }
    rt.warm_inflight = true;
    true
}

/// Port of `_schedule_backend_warm`. BL-R6-2a dedup: skip when a
/// warm-start for `name` is already pending, or when the backend is
/// already known-active (a fresh `last_active` entry, kept warm by
/// the `/api/` path) -- both keep a burst of shell-only GETs from
/// accumulating redundant tasks.
fn schedule_backend_warm(state: &Arc<RouterState>, name: &str) {
    let already_active = state
        .runtime
        .snapshot(name)
        .is_some_and(|rt| rt.last_active.contains_key("backend"));
    if already_active {
        return;
    }
    let should_spawn = state.runtime.with_runtime_mut(name, should_spawn_warm);
    if should_spawn {
        tokio::spawn(warm_backend(Arc::clone(state), name.to_string()));
    }
}

/// Shared body for both `dashboard_handler` route registrations below
/// -- axum has no "optional trailing wildcard" extractor, so the
/// bare-trailing-slash form (`rest = ""`) and the `{*rest}` form are
/// two distinct routes/handlers sharing this one implementation, both
/// ultimately porting the same Python `dashboard_handler`.
///
/// SC-R6-1: the warm-start side effect is gated on `warm_authorized`
/// (threaded from `session_gate_layer`'s own decision, see
/// `middleware::WarmAuthorized`'s doc) -- serving the response itself
/// stays a uniform 200/404 shell for member/non-member/bogus slug
/// alike; only the spawn is authorization-gated, closing the
/// arbitrary-tenant-activation gap a plain `GET /agent-mcp/app/<victim>/`
/// would otherwise open for any authenticated non-member.
// 8 args: private, single-call-shape helper shared by the two real
// axum handlers below (bare `/app/{name}/` vs `/app/{name}/{*rest}`,
// axum's own lack of an optional-trailing-wildcard extractor forces
// two entry points) -- a struct would just move the same 8 fields
// one level of indirection without adding a real invariant to name.
#[allow(clippy::too_many_arguments)]
async fn dashboard_handler_impl(
    state: &Arc<RouterState>,
    addr: SocketAddr,
    uri_path: &str,
    uri_query: Option<&str>,
    headers: &HeaderMap,
    name: &str,
    warm_authorized: bool,
    rest: &str,
) -> Response {
    // Phase F (prancy-napping-pie): W1 single-tenant redirect --
    // port of `dashboard_handler`'s own `_maybe_single_tenant_redirect`
    // call site (`agent_mcp/router/app.py`, the third of three; the
    // other two -- `backend_mcp_handler`/`backend_api_handler` -- were
    // already ported via `mcp_handler::maybe_single_tenant_redirect`,
    // this dashboard call site was missed). Checked before the warm
    // start / dashboard-file serve so a wrong-name URL never spawns a
    // backend or serves a shell for the wrong project.
    if !name.is_empty() {
        if let Some(resp) = crate::mcp_handler::maybe_single_tenant_redirect(
            &state.mcp_handler_config,
            name,
            uri_path,
            uri_query,
        ) {
            return resp.into_response();
        }
    }
    if !name.is_empty() && warm_authorized {
        schedule_backend_warm(state, name);
    }
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
    Extension(WarmAuthorized(warm_authorized)): Extension<WarmAuthorized>,
    Path(name): Path<String>,
) -> Response {
    dashboard_handler_impl(
        &state,
        addr,
        uri.path(),
        uri.query(),
        &headers,
        &name,
        warm_authorized,
        "",
    )
    .await
}

/// Port of `dashboard_handler` for `GET /agent-mcp/app/{name}/{*rest}`.
pub async fn dashboard_handler(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    OriginalUri(uri): OriginalUri,
    headers: HeaderMap,
    Extension(WarmAuthorized(warm_authorized)): Extension<WarmAuthorized>,
    Path((name, rest)): Path<(String, String)>,
) -> Response {
    dashboard_handler_impl(
        &state,
        addr,
        uri.path(),
        uri.query(),
        &headers,
        &name,
        warm_authorized,
        &rest,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orchestrator::ensure::EnsureConfig;
    use crate::orchestrator::runtime::RuntimeStore;
    use crate::project_registry::ProjectRegistry;
    use crate::rate_limit::RateLimitConfig;
    use crate::state::{RouterState, RouterStateConfig};
    use conexus_db::schema::init_router_schema;

    async fn real_state() -> (tempfile::TempDir, Arc<RouterState>) {
        let dir = tempfile::TempDir::new().unwrap();
        let conn = rusqlite::Connection::open_in_memory().unwrap();
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

    /// Same as [`real_state`], configured for single-tenant mode
    /// (Phase F regression coverage: `dashboard_handler`'s own W1
    /// redirect call site was missed when this file was ported --
    /// `mcp_handler::maybe_single_tenant_redirect` existed and was
    /// tested for the other two call sites, but this one just served
    /// the wrong project's shell with a 200 instead of redirecting).
    async fn real_state_single_tenant(name: &str) -> (tempfile::TempDir, Arc<RouterState>) {
        let dir = tempfile::TempDir::new().unwrap();
        let conn = rusqlite::Connection::open_in_memory().unwrap();
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
                single_tenant_name: Some(name.to_string()),
                single_tenant_workspace: None,
                max_streams_per_agent: 4,
                max_streams_global: 64,
                default_workspace_parent: dir.path().join("projects"),
                token_dir: None,
            },
        ));
        (dir, state)
    }

    #[tokio::test]
    async fn dashboard_handler_impl_w1_redirects_a_wrong_project_name_in_single_tenant_mode() {
        let (_dir, state) = real_state_single_tenant("only-project").await;
        let resp = dashboard_handler_impl(
            &state,
            addr(),
            "/agent-mcp/app/wrong-name/",
            None,
            &HeaderMap::new(),
            "wrong-name",
            true,
            "",
        )
        .await;
        assert_eq!(resp.status(), 302);
        let location = resp
            .headers()
            .get(header::LOCATION)
            .and_then(|v| v.to_str().ok())
            .unwrap();
        assert_eq!(location, "/agent-mcp/app/only-project/");
        // A real gap this same missing check would have opened: the
        // wrong-name path must NEVER warm-start a backend either.
        assert!(state.runtime.snapshot("wrong-name").is_none());
    }

    #[tokio::test]
    async fn dashboard_handler_impl_serves_normally_for_the_configured_single_tenant_project() {
        let (_dir, state) = real_state_single_tenant("only-project").await;
        let resp = dashboard_handler_impl(
            &state,
            addr(),
            "/agent-mcp/app/only-project/",
            None,
            &HeaderMap::new(),
            "only-project",
            true,
            "",
        )
        .await;
        // No dashboard_dir configured in this fixture -> 404, not a
        // redirect -- proves the CORRECT name falls through to the
        // ordinary serve path instead of being redirected.
        assert_eq!(resp.status(), 404);
    }

    #[test]
    fn should_spawn_warm_admits_the_first_caller_and_marks_inflight() {
        let store = RuntimeStore::new();
        let spawned = store.with_runtime_mut("proj-a", should_spawn_warm);
        assert!(spawned, "the first caller must be the one to spawn");
        let rt = store.snapshot("proj-a").expect("row created lazily");
        assert!(rt.warm_inflight, "warm_inflight must now be set");
    }

    #[test]
    fn should_spawn_warm_dedups_a_concurrent_second_caller() {
        let store = RuntimeStore::new();
        let first = store.with_runtime_mut("proj-a", should_spawn_warm);
        let second = store.with_runtime_mut("proj-a", should_spawn_warm);
        assert!(first);
        assert!(
            !second,
            "BL-R6-2a: a warm-start already pending must not spawn a second one"
        );
    }

    #[test]
    fn should_spawn_warm_admits_again_once_the_prior_one_cleared_the_flag() {
        let store = RuntimeStore::new();
        assert!(store.with_runtime_mut("proj-a", should_spawn_warm));
        // Port of `warm_backend`'s own completion clear.
        store.with_runtime_mut("proj-a", |rt| rt.warm_inflight = false);
        assert!(
            store.with_runtime_mut("proj-a", should_spawn_warm),
            "a genuinely NEW request after the prior warm-start finished must spawn again"
        );
    }

    #[tokio::test]
    async fn schedule_backend_warm_skips_a_project_already_known_active() {
        let (_dir, state) = real_state().await;
        state.runtime.with_runtime_mut("proj-a", |rt| {
            rt.last_active
                .insert("backend".to_string(), std::time::SystemTime::now());
        });
        schedule_backend_warm(&state, "proj-a");
        // Dedup-by-already-active must not even touch `warm_inflight`.
        let rt = state.runtime.snapshot("proj-a").unwrap();
        assert!(!rt.warm_inflight);
    }

    #[tokio::test]
    async fn schedule_backend_warm_marks_inflight_for_a_not_yet_active_project() {
        let (_dir, state) = real_state().await;
        // A single-threaded runtime: the spawned task cannot run until
        // this test itself yields/awaits, so the flag is still
        // observably `true` right after the call returns.
        schedule_backend_warm(&state, "unknown-proj");
        let rt = state.runtime.snapshot("unknown-proj").unwrap();
        assert!(
            rt.warm_inflight,
            "the spawned task hasn't run yet on a single-threaded runtime"
        );
    }

    // -- SC-R6-1: warm-start gated on the session-gate's own authorization
    // decision, not merely project existence ------------------------------

    fn addr() -> SocketAddr {
        "127.0.0.1:9999".parse().unwrap()
    }

    #[tokio::test]
    async fn dashboard_handler_impl_does_not_warm_when_not_authorized() {
        let (_dir, state) = real_state().await;
        // Mirrors an authenticated NON-MEMBER's `GET /app/<victim>/`:
        // `session_gate_layer` maps that to `WarmAuthorized(false)` (see
        // that type's own doc) -- the shell is still served (asserted via
        // the 404 fast path since no dashboard_dir is configured here;
        // the response shape itself is covered by `serve_candidate`'s own
        // tests), but the spawn side effect must not fire.
        let _ = dashboard_handler_impl(
            &state,
            addr(),
            "/agent-mcp/app/victim/",
            None,
            &HeaderMap::new(),
            "victim",
            false,
            "",
        )
        .await;
        match state.runtime.snapshot("victim") {
            None => {} // never even touched -- also a valid "did not warm"
            Some(rt) => assert!(
                !rt.warm_inflight,
                "a non-member/unauthorized GET must NOT warm-start the backend \
                 -- that is a cross-tenant resource-activation / DoS vector"
            ),
        }
    }

    #[tokio::test]
    async fn dashboard_handler_impl_warms_when_authorized() {
        let (_dir, state) = real_state().await;
        // Mirrors an authorized member/sysadmin's `GET /app/<name>/`:
        // `session_gate_layer` maps that to `WarmAuthorized(true)`.
        let _ = dashboard_handler_impl(
            &state,
            addr(),
            "/agent-mcp/app/shared/",
            None,
            &HeaderMap::new(),
            "shared",
            true,
            "",
        )
        .await;
        let rt = state
            .runtime
            .snapshot("shared")
            .expect("schedule_backend_warm must have touched the runtime row");
        assert!(
            rt.warm_inflight,
            "an authorized GET must schedule the warm-start \
             (the documented lazy-spawn on first request)"
        );
    }
}
