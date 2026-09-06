//! Real axum middleware wrapping the already-ported process-wide gate
//! decisions. Phase E2, `conexus-router-security-middleware` (PR23
//! step 2 of the 10-PR app-wiring breakdown). Layered in the SAME
//! order Python's real `app.py` middleware chain runs: security
//! headers (outermost -- touches every response, even one an inner
//! layer rejects) -> rate-limit -> empty-users-redirect ->
//! session-gate (innermost, closest to the real handler).
//!
//! **Deliberately layered onto an otherwise-empty router first**
//! (matching `conexus-backend`'s own PR1-shaped "verify the gate in
//! isolation" precedent) -- no real admin/proxy route exists yet
//! (later steps in the same breakdown); this PR's own live
//! verification (boot the real compiled binary, curl it) exercises
//! the gates directly against `GET /health`. `axum::middleware::Next`
//! has no public constructor anywhere in this workspace's pinned
//! axum version (confirmed by reading the vendored source, not
//! assumed) -- unlike every framework-agnostic decision function
//! elsewhere in this crate, these four functions are NOT unit-tested
//! directly; the pure sub-logic they compose (`peer_info`,
//! `is_request_trusted`) is, and the full stack is proven end-to-end
//! against the real binary instead, matching every other axum-facing
//! PR in this migration's own established discipline.
//!
//! **Genuinely new primitive**: [`peer_info`] is the first real
//! extraction of [`rate_limit::PeerInfo`] off an actual connection.
//! This binary only ever binds TCP (never a UDS listener itself --
//! that's the proxy's OWN outbound connection to a per-project
//! backend, a completely different socket), so `uds_uid` is always
//! `None` here; the `own_uid`/`extra_trusted_uids` SO_PEERCRED
//! parameters `PeerInfo::is_trusted` still takes are consequently
//! inert for every real connection this process accepts, kept as
//! explicit `0`/empty-set constants rather than plumbing a real UID
//! lookup nothing would ever exercise.
//!
//! **Proxy-header SSO is deliberately NOT wired into the session
//! gate here** -- PR21's own module doc says its caller "resolves
//! proxy-header identity FIRST and only calls into this gate on a
//! miss"; that composition is step 10 (`sso-admin-config`) of this
//! same breakdown, once the whole route table exists to reason about
//! which paths need it.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, Request, State};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use chrono::Utc;

use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::rate_limit::{self, PeerInfo};
use crate::security_headers;
use crate::session_gate::{self, GateRequest, SessionGateOutcome};
use crate::state::RouterState;
use crate::{identity, login, mount};

/// SO_PEERCRED parameters, inert for this binary -- see module doc.
const OWN_UID: u32 = 0;

/// Port of Python's `request["_warm_authorized"]` stash -- carries
/// `session_gate_layer`'s own authorization decision (see the match
/// arms below) forward as a request extension so `dashboard_handlers.rs`
/// can gate the `_schedule_backend_warm` side effect on it without
/// re-deriving the session-gate decision a second time. `pub(crate)`:
/// produced here, consumed in a different module.
#[derive(Debug, Clone, Copy)]
pub(crate) struct WarmAuthorized(pub bool);

fn extra_trusted_uids() -> std::collections::HashSet<u32> {
    std::collections::HashSet::new()
}

/// `pub(crate)`, not private -- `dashboard_handlers.rs` needs the same
/// per-connection trust resolution the middleware layers use, to
/// compute the correct external asset prefix for a dashboard-file
/// serve (matching Python's own per-request `mount.external_prefix(req)`
/// calls in `dashboard_handler`/`overview_dashboard_handler`/
/// `dashboard_assets_handler`).
pub(crate) fn peer_info(addr: SocketAddr) -> PeerInfo {
    PeerInfo {
        tcp_ip: Some(addr.ip()),
        uds_uid: None,
    }
}

/// `pub(crate)`, not private -- the lifecycle-rest/users-groups-rest
/// handlers (a different module) need this to pull the `Cookie`
/// header for `perm_gates::RevalidationSpec.cookie_header`.
pub(crate) fn header_str<'a>(req: &'a Request, name: &str) -> Option<&'a str> {
    req.headers().get(name).and_then(|v| v.to_str().ok())
}

pub(crate) fn is_request_trusted(state: &RouterState, peer: &PeerInfo) -> bool {
    peer.is_trusted(
        &state.rate_limit_config.trusted_proxies,
        OWN_UID,
        &extra_trusted_uids(),
    )
}

/// Port of `security_headers_middleware`. Runs OUTERMOST (added last
/// via `.layer()`) so it can fill in headers on ANY response,
/// including one an inner layer rejected with. This binary terminates
/// no TLS itself (always fronted by a reverse proxy in every real
/// deployment, matching Python's own posture) -- `url_scheme` is
/// always `"http"`; a trusted proxy's `X-Forwarded-Proto` is what
/// actually flips `is_https` in practice.
pub async fn security_headers_layer(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    req: Request,
    next: Next,
) -> Response {
    let peer = peer_info(addr);
    let is_trusted = is_request_trusted(&state, &peer);
    let forwarded_proto = header_str(&req, "x-forwarded-proto");
    let is_https = security_headers::request_is_https(is_trusted, forwarded_proto, "http");
    let csp_value = security_headers::csp(None);

    let mut response = next.run(req).await;
    let headers = response.headers_mut();
    for header in security_headers::security_headers(is_https, &csp_value) {
        let Ok(name) = axum::http::HeaderName::try_from(header.name) else {
            continue;
        };
        let Ok(value) = axum::http::HeaderValue::try_from(header.value.as_str()) else {
            continue;
        };
        if header.overwrite {
            headers.insert(name, value);
        } else {
            headers.entry(name).or_insert(value);
        }
    }
    response
}

/// Port of `rate_limit_middleware`.
pub async fn rate_limit_layer(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    req: Request,
    next: Next,
) -> Response {
    let peer = peer_info(addr);
    let xff = header_str(&req, "x-forwarded-for");
    let client_ip = rate_limit::resolve_client_ip(
        &peer,
        xff,
        &state.rate_limit_config.trusted_proxies,
        OWN_UID,
        &extra_trusted_uids(),
    );
    let path = mount::canonical_path(req.uri().path());
    let method = req.method().as_str().to_uppercase();
    let denied = {
        let mut rl_state = state
            .rate_limit_state
            .lock()
            .expect("rate limit mutex poisoned");
        rate_limit::check_rate_limit(
            &mut rl_state,
            &state.rate_limit_config,
            &client_ip,
            &path,
            &method,
            std::time::Instant::now(),
        )
    };
    match denied {
        Some(resp) => resp.into_response(),
        None => next.run(req).await,
    }
}

/// Port of `empty_users_redirect_middleware`.
pub async fn empty_users_redirect_layer(
    State(state): State<Arc<RouterState>>,
    req: Request,
    next: Next,
) -> Response {
    let path = mount::canonical_path(req.uri().path());
    let users_empty = {
        let conn = state.conn.lock().await;
        identity::users_table_is_empty(&conn).unwrap_or(false)
    };
    if login::should_redirect_to_setup(&path, users_empty) {
        return HandlerResponse {
            status: 303,
            headers: vec![("Location".to_string(), "/agent-mcp/setup".to_string())],
            body: HandlerBody::Empty,
        }
        .into_response();
    }
    next.run(req).await
}

/// Port of `require_operator_session_middleware`'s cookie-only path
/// (see module doc for the proxy-header-fallback deferral).
pub async fn session_gate_layer(
    State(state): State<Arc<RouterState>>,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    mut req: Request,
    next: Next,
) -> Response {
    let peer = peer_info(addr);
    let is_trusted = is_request_trusted(&state, &peer);
    let path = mount::canonical_path(req.uri().path());
    let raw_path_qs = req
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| req.uri().path().to_string());
    let method = req.method().as_str().to_uppercase();
    let accept_header = header_str(&req, "accept").map(str::to_string);
    let cookie_header = header_str(&req, "cookie").map(str::to_string);
    let forwarded_prefix = header_str(&req, "x-forwarded-prefix").map(str::to_string);
    let login_url = mount::external_path(&path, is_trusted, forwarded_prefix.as_deref(), "/login");

    let outcome = {
        let conn = state.conn.lock().await;
        let gate_req = GateRequest {
            path: &path,
            raw_path_qs: &raw_path_qs,
            method: &method,
            accept_header: accept_header.as_deref(),
            cookie_header: cookie_header.as_deref(),
            login_url: &login_url,
        };
        session_gate::evaluate_session_gate(
            &conn,
            &state.registry,
            &state.session_gate_config,
            Utc::now(),
            &gate_req,
        )
    };

    match outcome {
        Ok(SessionGateOutcome::PassThrough { warm_authorized }) => {
            req.extensions_mut().insert(WarmAuthorized(warm_authorized));
            next.run(req).await
        }
        Ok(SessionGateOutcome::PublicAppShell) => {
            req.extensions_mut().insert(WarmAuthorized(false));
            next.run(req).await
        }
        Ok(SessionGateOutcome::Reject(resp)) => resp.into_response(),
        Ok(SessionGateOutcome::Allow(identity)) => {
            // SC-R6-1: reaching `Allow` means sysadmin or a
            // sufficient project role -- exactly the two branches
            // Python's own `request["_warm_authorized"] = True` stash
            // covers on this path (see `dashboard_static.rs`'s own
            // module doc for the full mapping).
            req.extensions_mut().insert(WarmAuthorized(true));
            req.extensions_mut().insert(*identity);
            next.run(req).await
        }
        Err(_) => HandlerResponse {
            status: 500,
            headers: Vec::new(),
            body: HandlerBody::Text("internal error resolving session".to_string()),
        }
        .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orchestrator::ensure::EnsureConfig;
    use crate::project_registry::ProjectRegistry;
    use crate::rate_limit::RateLimitConfig;
    use crate::state::RouterStateConfig;
    use conexus_db::schema::init_router_schema;

    // -- pure sub-logic --------------------------------------------------

    #[test]
    fn peer_info_extracts_the_tcp_ip_and_leaves_uds_uid_none() {
        let addr: SocketAddr = "10.0.0.5:12345".parse().unwrap();
        let peer = peer_info(addr);
        assert_eq!(peer.tcp_ip, Some(addr.ip()));
        assert!(peer.uds_uid.is_none());
    }

    #[test]
    fn is_request_trusted_reflects_the_configured_allowlist() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        init_router_schema(&conn).unwrap();
        let dir = tempfile::TempDir::new().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let mut rate_limit_config = RateLimitConfig::resolve(|_| None);
        rate_limit_config.trusted_proxies =
            std::collections::HashSet::from(["10.0.0.5".parse().unwrap()]);
        let state = RouterState::new(
            conn,
            registry,
            rate_limit_config,
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
        );
        let trusted_peer = peer_info("10.0.0.5:1".parse().unwrap());
        let untrusted_peer = peer_info("203.0.113.9:1".parse().unwrap());
        assert!(is_request_trusted(&state, &trusted_peer));
        assert!(!is_request_trusted(&state, &untrusted_peer));
    }
}
