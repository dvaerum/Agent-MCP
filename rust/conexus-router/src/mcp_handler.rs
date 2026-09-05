//! The router's `/mcp` + `/api/*` HTTP handler layer -- port of
//! `agent_mcp/router/app.py`'s `backend_mcp_handler`/
//! `backend_api_handler` (Phase E2 PR 9). Framework-agnostic, like
//! `proxy_core.rs`: no axum types here, so app-wiring (PR 23) converts
//! this module's plain Rust request/response shapes into real axum
//! extractors/responses -- these two handler functions are the first
//! REAL callers of `proxy_core`/`orchestrator`/`project_registry`.
//!
//! **Scope, matching PR 8's own decision**: the BEARER-authenticated
//! path only. Every branch that needs a cookie-authenticated operator
//! session (`_forwarding_header_from_cookie`, the REST-side operator-
//! session gate) is out of scope until that identity plumbing is
//! ported. A caller with no bearer at all gets the SAME uniform 401
//! Python's own no-credential branch returns -- there is no cookie
//! fallback to attempt yet, matching Python's OWN behavior for a
//! caller carrying neither credential.
//!
//! **SEC FINDING 1 (constant-time pre-auth 401 floor) is preserved
//! bit-for-bit**: an UNKNOWN project (resolved in-process, fast) and a
//! KNOWN project whose bearer the backend rejects (a full UDS round-
//! trip, slower) must both return their 401 at ~the same wall-clock
//! time, or a not-yet-authenticated caller can enumerate valid
//! project names by response latency. [`floored_unauthorized`] is the
//! one function every pre-auth-401 path in [`backend_mcp_handler`]
//! funnels through.

// No axum-route caller yet -- app-wiring (PR 23) is the first real
// consumer, same helpers-ahead-of-their-first-consumer precedent as
// every other not-yet-wired module in this crate.
#![allow(dead_code)]

use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant};

use bytes::Bytes;
use chrono::Utc;
use hyper::{HeaderMap, Method};
use regex::Regex;

use crate::orchestrator::ensure::{EnsureConfig, EnsureError};
use crate::orchestrator::resolve::{self, ResolveError};
use crate::orchestrator::runtime::{EnsureFailureReason, RuntimeStore};
use crate::path_policy;
use crate::project_registry::ProjectRegistry;
use crate::proxy_core::{
    self, AliasInfo, ProxyError, ProxyRequest, ProxyResponseBody, StreamCapRegistry,
};

/// Every env/config knob these handlers read -- port of the module-
/// level SEC-finding constants (`_PREAUTH_401_FLOOR_SEC`,
/// `_MCP_MAX_BODY_BYTES`) plus `SINGLE_TENANT_NAME`, unified into one
/// explicit struct rather than scattered globals (this crate's own
/// convention).
#[derive(Debug, Clone)]
pub struct McpHandlerConfig {
    pub single_tenant_name: Option<String>,
    pub preauth_401_floor: Duration,
    pub mcp_max_body_bytes: usize,
}

impl Default for McpHandlerConfig {
    fn default() -> Self {
        Self {
            single_tenant_name: None,
            preauth_401_floor: Duration::from_millis(50),
            mcp_max_body_bytes: 1024 * 1024,
        }
    }
}

/// A handler's response, in plain Rust terms -- app-wiring converts
/// this into a real axum `Response`.
pub struct HandlerResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: HandlerBody,
}

pub enum HandlerBody {
    Empty,
    Text(String),
    Json(serde_json::Value),
    Proxied(ProxyResponseBody),
}

impl std::fmt::Debug for HandlerBody {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HandlerBody::Empty => f.write_str("Empty"),
            HandlerBody::Text(s) => f.debug_tuple("Text").field(s).finish(),
            HandlerBody::Json(v) => f.debug_tuple("Json").field(v).finish(),
            HandlerBody::Proxied(_) => f.write_str("Proxied(..)"),
        }
    }
}

impl std::fmt::Debug for HandlerResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HandlerResponse")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .field("body", &self.body)
            .finish()
    }
}

impl HandlerResponse {
    fn proxied(resp: proxy_core::ProxyResponse) -> Self {
        Self {
            status: resp.status,
            headers: resp.headers,
            body: HandlerBody::Proxied(resp.body),
        }
    }
}

/// Port of `_BEARER_RE`/`_extract_bearer`.
static BEARER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\s*Bearer\s+([A-Za-z0-9._-]+)\s*$").unwrap());

pub fn extract_bearer(headers: &HeaderMap) -> Option<String> {
    let raw = headers.get(hyper::header::AUTHORIZATION)?.to_str().ok()?;
    BEARER_RE.captures(raw).map(|c| c[1].to_string())
}

/// The uniform, un-floored pre-auth 401 body -- port of
/// `_unauthorized()`.
fn unauthorized_response() -> HandlerResponse {
    HandlerResponse {
        status: 401,
        headers: vec![(
            "WWW-Authenticate".to_string(),
            "Bearer realm=\"agent-mcp\"".to_string(),
        )],
        body: HandlerBody::Text("invalid or missing agent bearer token".to_string()),
    }
}

/// Port of `_floored_unauthorized` -- sleeps out the remainder of
/// `cfg.preauth_401_floor` measured from `t0` before returning the
/// canonical 401. See the module doc for why this matters.
pub async fn floored_unauthorized(cfg: &McpHandlerConfig, t0: Instant) -> HandlerResponse {
    let elapsed = t0.elapsed();
    if elapsed < cfg.preauth_401_floor {
        tokio::time::sleep(cfg.preauth_401_floor - elapsed).await;
    }
    unauthorized_response()
}

/// Port of `_maybe_single_tenant_redirect`/`_w1_redirect`. `path` is
/// the ORIGINAL request path (before any project-name substitution);
/// the replacement substitutes only the FIRST occurrence of `name`,
/// matching decision #9 (W1)'s section-path-preserving shape.
pub fn maybe_single_tenant_redirect(
    cfg: &McpHandlerConfig,
    name: &str,
    path: &str,
    query: Option<&str>,
) -> Option<HandlerResponse> {
    let single = cfg.single_tenant_name.as_deref()?;
    if name == single {
        return None;
    }
    let mut new_path = path.replacen(name, single, 1);
    if let Some(q) = query {
        if !q.is_empty() {
            new_path = format!("{new_path}?{q}");
        }
    }
    Some(HandlerResponse {
        status: 302,
        headers: vec![
            ("Location".to_string(), new_path),
            ("Cache-Control".to_string(), "no-store".to_string()),
        ],
        body: HandlerBody::Empty,
    })
}

/// Port of the `API_VERSION_*`/`API_MEDIA_TYPE` constants.
pub const API_VERSION_CURRENT: &str = "v1";
pub const API_MEDIA_TYPE: &str = "application/vnd.agent-mcp.v1+json";
const API_DOCS_URL: &str = "https://github.com/dvaerum/Agent-MCP/blob/main/docs/api-versioning.md";

/// Port of `_accept_includes_strict_api_media` -- deliberately no
/// wildcard honouring (`*/*`/`application/json` don't count); an
/// explicit opt-in is the whole point of the gate.
pub fn accept_includes_strict_api_media(accept_header: &str) -> bool {
    if accept_header.is_empty() {
        return false;
    }
    accept_header.split(',').any(|part| {
        part.split(';')
            .next()
            .unwrap_or("")
            .trim()
            .eq_ignore_ascii_case(API_MEDIA_TYPE)
    })
}

/// Port of `_api_version_required_response` -- the 406 body shape.
/// `pub(crate)`: `session_gate.rs`'s `unknown_project_response` reuses
/// this verbatim (Python's own `app.unknown_project_response` docstring
/// says it "reproduces `backend_api_handler`'s own decision ORDER so
/// the two cases stay byte-identical" -- reusing the SAME function is
/// how a Rust port keeps that guarantee, rather than a second,
/// independently-maintained copy of this JSON shape).
pub(crate) fn api_version_required_response() -> HandlerResponse {
    HandlerResponse {
        status: 406,
        headers: vec![],
        body: HandlerBody::Json(serde_json::json!({
            "error": "version_required",
            "message": format!(
                "agent-mcp REST endpoints require an Accept header specifying the API version. Resend with: Accept: {API_MEDIA_TYPE}"
            ),
            "supported_versions": [API_VERSION_CURRENT],
            "current_default": API_VERSION_CURRENT,
            "docs": API_DOCS_URL,
        })),
    }
}

/// Map an [`EnsureError`] to `(status, message)` -- port of the
/// status/reason pairs `_ensure`'s own raised `web.HTTP*` exceptions
/// carry (see `orchestrator::ensure`'s own doc for the underlying
/// cases).
fn ensure_error_status(e: &EnsureError) -> (u16, String) {
    match e {
        EnsureError::UnknownProject => (404, "unknown project".to_string()),
        EnsureError::Cooldown(reason) => (504, reason.message().to_string()),
        EnsureError::Failed(EnsureFailureReason::SystemctlFailed) => (
            500,
            EnsureFailureReason::SystemctlFailed.message().to_string(),
        ),
        EnsureError::Failed(EnsureFailureReason::SocketTimeout) => (
            504,
            EnsureFailureReason::SocketTimeout.message().to_string(),
        ),
        EnsureError::Registry(reg) => (500, reg.to_string()),
        EnsureError::UnitName(u) => (500, u.to_string()),
        EnsureError::Io(io) => (500, io.to_string()),
    }
}

/// Map any [`ProxyError`] to a genuine [`HandlerResponse`] -- the
/// SHARED, un-floored mapping every proxy failure gets by default;
/// [`backend_mcp_handler`] additionally floors two SPECIFIC cases
/// (body-too-large, backend-401) on top of this, matching Python's
/// own narrow `except web.HTTPRequestEntityTooLarge` + `if resp.status
/// == 401` collapses -- everything else propagates through this
/// mapping unfloored, exactly as an uncaught Python exception would.
fn proxy_error_response(e: ProxyError) -> HandlerResponse {
    match e {
        ProxyError::Ensure(inner) => {
            let (status, message) = ensure_error_status(&inner);
            HandlerResponse {
                status,
                headers: vec![],
                body: HandlerBody::Text(message),
            }
        }
        ProxyError::BackendUnavailable(_) => HandlerResponse {
            status: 502,
            headers: vec![("Retry-After".to_string(), "2".to_string())],
            body: HandlerBody::Text("Backend temporarily unavailable; retry shortly.".to_string()),
        },
        ProxyError::TooManyStreams => HandlerResponse {
            status: 429,
            headers: vec![("Retry-After".to_string(), "5".to_string())],
            body: HandlerBody::Text("Too many concurrent streams; retry shortly.".to_string()),
        },
        ProxyError::ClientGone => HandlerResponse {
            status: 499,
            headers: vec![],
            body: HandlerBody::Empty,
        },
        ProxyError::Registry(reg) => HandlerResponse {
            status: 500,
            headers: vec![],
            body: HandlerBody::Text(reg.to_string()),
        },
        ProxyError::UdsClient(err) => HandlerResponse {
            status: 502,
            headers: vec![],
            body: HandlerBody::Text(err.to_string()),
        },
        ProxyError::UpstreamStream(err) => HandlerResponse {
            status: 502,
            headers: vec![],
            body: HandlerBody::Text(err.to_string()),
        },
        ProxyError::Http(err) => HandlerResponse {
            status: 500,
            headers: vec![],
            body: HandlerBody::Text(err.to_string()),
        },
    }
}

/// The inbound HTTP request, translated into this handler layer's own
/// plain shape -- `body` is ALREADY buffered (see `proxy_core`'s own
/// doc on why that's structural, not a convention, throughout this
/// crate's proxy surface).
pub struct HandlerRequest {
    pub method: Method,
    /// The URL's project-name segment (`req.match_info["name"]`).
    pub project_name: String,
    /// The full original request path, used only for the single-
    /// tenant redirect's substring replacement.
    pub path: String,
    pub query: Option<String>,
    pub headers: HeaderMap,
    pub body: Bytes,
}

fn path_and_query(path: &str, query: Option<&str>) -> String {
    match query {
        Some(q) if !q.is_empty() => format!("{path}?{q}"),
        _ => path.to_string(),
    }
}

/// `/agent-mcp/<name>/mcp` -> backend `/mcp`. Port of
/// `backend_mcp_handler`'s bearer-authenticated path -- see the module
/// doc for the cookie-path scope this deliberately omits.
#[allow(clippy::too_many_arguments)]
pub async fn backend_mcp_handler(
    store: &RuntimeStore,
    stream_caps: &Arc<StreamCapRegistry>,
    registry: &ProjectRegistry,
    sock_dir: &std::path::Path,
    ensure_cfg: &EnsureConfig,
    cfg: &McpHandlerConfig,
    now: chrono::DateTime<Utc>,
    req: HandlerRequest,
) -> HandlerResponse {
    let t0 = Instant::now();

    if let Some(redirect) =
        maybe_single_tenant_redirect(cfg, &req.project_name, &req.path, req.query.as_deref())
    {
        return redirect;
    }

    let bearer = extract_bearer(&req.headers);

    // SEC (auth-before-resolve, owner-authorised): gate on credential
    // PRESENCE before ever resolving the project, so an unknown
    // project and a known-but-uncredentialed one are indistinguishable
    // to an anonymous caller. This crate has no cookie fallback yet
    // (see the module doc), so "no bearer" is unconditionally the
    // uniform 401 -- exactly Python's own outcome for a caller with
    // NEITHER credential.
    if bearer.is_none() {
        return floored_unauthorized(cfg, t0).await;
    }

    // Method whitelist. A bearer is guaranteed present at this point,
    // so the GET-requires-bearer branch Python has is a no-op here;
    // only the verb set itself needs checking.
    if !matches!(req.method, Method::POST | Method::GET | Method::DELETE) {
        return HandlerResponse {
            status: 405,
            headers: vec![("Allow".to_string(), "POST, GET, DELETE".to_string())],
            body: HandlerBody::Text("/mcp accepts only POST, GET, or DELETE".to_string()),
        };
    }

    // SEC5 project-existence oracle: an unauthenticated caller must
    // never learn "unknown project" (404) vs "known project, bad
    // bearer" (401) by status code -- collapse the resolve failure
    // into the SAME floored 401 every other pre-auth failure returns.
    let (real_name, alias) = match resolve::resolve(registry, &req.project_name, now) {
        Ok(r) => r,
        Err(ResolveError::UnknownProject) => return floored_unauthorized(cfg, t0).await,
        Err(ResolveError::Registry(e)) => {
            return HandlerResponse {
                status: 500,
                headers: vec![],
                body: HandlerBody::Text(e.to_string()),
            }
        }
    };
    let alias_info = alias.map(|(name, expires_at)| AliasInfo { name, expires_at });

    // SEC FINDING 2: an oversized body 413-vs-401 is itself a project-
    // existence oracle for a not-yet-authenticated caller (only a
    // KNOWN project ever reaches the body-size check) -- collapse into
    // the same floored 401.
    if req.body.len() > cfg.mcp_max_body_bytes {
        return floored_unauthorized(cfg, t0).await;
    }

    let proxy_req = ProxyRequest {
        method: req.method,
        path_and_query: path_and_query("/mcp", req.query.as_deref()),
        headers: req.headers,
        body: req.body,
    };

    match proxy_core::proxy_to_backend(
        store,
        stream_caps,
        registry,
        sock_dir,
        &real_name,
        ensure_cfg,
        proxy_req,
        alias_info.as_ref(),
    )
    .await
    {
        Ok(resp) if resp.status == 401 => {
            // SEC5 401-envelope parity: a bearer the backend rejects
            // always means "not-yet-authenticated" on this transport
            // -- collapse into the router's own canonical 401 so a
            // known-but-rejected project and an unknown one are
            // byte-indistinguishable (same status/reason/WWW-
            // Authenticate/body; the backend's own richer envelope and
            // `Server` fingerprint are dropped).
            floored_unauthorized(cfg, t0).await
        }
        Ok(resp) => HandlerResponse::proxied(resp),
        Err(e) => proxy_error_response(e),
    }
}

/// `/agent-mcp/__api/<name>/{rest}` -> backend `/api/{rest}`. Port of
/// `backend_api_handler` -- see the module doc for the cookie-path
/// scope this deliberately omits (this handler never had any
/// bearer-vs-cookie branching in Python either; it forwards whatever
/// credential state the caller already carries and lets the backend's
/// own auth surface decide).
#[allow(clippy::too_many_arguments)]
pub async fn backend_api_handler(
    store: &RuntimeStore,
    stream_caps: &Arc<StreamCapRegistry>,
    registry: &ProjectRegistry,
    sock_dir: &std::path::Path,
    ensure_cfg: &EnsureConfig,
    cfg: &McpHandlerConfig,
    now: chrono::DateTime<Utc>,
    rest: &str,
    req: HandlerRequest,
) -> HandlerResponse {
    let accept = req
        .headers
        .get(hyper::header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let is_event_stream =
        rest == "events" || accept.to_ascii_lowercase().contains("text/event-stream");
    let is_delivery = path_policy::is_delivery(&req.project_name, rest);
    if req.method != Method::OPTIONS
        && !is_event_stream
        && !is_delivery
        && !accept_includes_strict_api_media(accept)
    {
        return api_version_required_response();
    }

    if let Some(redirect) =
        maybe_single_tenant_redirect(cfg, &req.project_name, &req.path, req.query.as_deref())
    {
        return redirect;
    }

    let (real_name, alias) = match resolve::resolve(registry, &req.project_name, now) {
        Ok(r) => r,
        Err(ResolveError::UnknownProject) => {
            return HandlerResponse {
                status: 404,
                headers: vec![],
                body: HandlerBody::Text("unknown project".to_string()),
            }
        }
        Err(ResolveError::Registry(e)) => {
            return HandlerResponse {
                status: 500,
                headers: vec![],
                body: HandlerBody::Text(e.to_string()),
            }
        }
    };
    let alias_info = alias.map(|(name, expires_at)| AliasInfo { name, expires_at });

    let proxy_req = ProxyRequest {
        method: req.method,
        path_and_query: path_and_query(&format!("/api/{rest}"), req.query.as_deref()),
        headers: req.headers,
        body: req.body,
    };

    match proxy_core::proxy_to_backend(
        store,
        stream_caps,
        registry,
        sock_dir,
        &real_name,
        ensure_cfg,
        proxy_req,
        alias_info.as_ref(),
    )
    .await
    {
        Ok(resp) => HandlerResponse::proxied(resp),
        Err(e) => proxy_error_response(e),
    }
}

/// Process-wide streaming-cap registry sizing -- port of
/// `MAX_STREAMS_PER_AGENT`/`MAX_STREAMS_GLOBAL`'s defaults. Kept here
/// (not in `proxy_core.rs`, which stays a pure library with no opinion
/// on real-process sizing) since it's the one place a real router
/// binary would construct its single, process-wide
/// `StreamCapRegistry` from.
pub const DEFAULT_MAX_STREAMS_PER_AGENT: u32 = 4;
pub const DEFAULT_MAX_STREAMS_GLOBAL: u32 = 64;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orchestrator::primitives::SystemctlMode;
    use crate::orchestrator::runtime::RuntimeStore;
    use http_body_util::Full;
    use hyper::header::{HeaderValue, AUTHORIZATION};
    use hyper::service::service_fn;
    use hyper::{Response, StatusCode};
    use hyper_util::rt::TokioIo;

    fn registry_with(dir: &std::path::Path, name: &str, backend_impl: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        let now: chrono::DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .register(name, "/ws/proj-a", backend_impl, now)
            .unwrap();
        registry
    }

    fn fast_ensure_cfg() -> EnsureConfig {
        EnsureConfig {
            systemctl_program: "true".to_string(),
            systemctl_mode: SystemctlMode::User,
            systemctl_timeout: Duration::from_secs(5),
            ensure_failure_cooldown: Duration::from_millis(200),
            boot_grace: Duration::from_millis(150),
            socket_poll_attempts: 5,
        }
    }

    fn fast_cfg() -> McpHandlerConfig {
        McpHandlerConfig {
            single_tenant_name: None,
            preauth_401_floor: Duration::from_millis(20),
            mcp_max_body_bytes: 1024 * 1024,
        }
    }

    /// A fixed instant every test resolves aliases/projects against --
    /// both handlers take `now` as an explicit parameter (this crate's
    /// own "never read a live clock inside business logic" convention,
    /// caught and fixed here after an earlier draft called `Utc::now()`
    /// internally and a test seeding an alias against a FIXED registry
    /// timestamp flaked against the REAL wall clock).
    fn test_now() -> chrono::DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }

    fn bearer_headers() -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, HeaderValue::from_static("Bearer tok123"));
        headers
    }

    fn base_req(project_name: &str, path: &str) -> HandlerRequest {
        HandlerRequest {
            method: Method::POST,
            project_name: project_name.to_string(),
            path: path.to_string(),
            query: None,
            headers: bearer_headers(),
            body: Bytes::from_static(b"{}"),
        }
    }

    async fn spawn_backend(
        sock_path: std::path::PathBuf,
        response_builder: impl Fn(&hyper::Request<hyper::body::Incoming>) -> Response<Full<Bytes>>
            + Send
            + Sync
            + 'static,
    ) {
        let listener = tokio::net::UnixListener::bind(&sock_path).unwrap();
        let response_builder = Arc::new(response_builder);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let response_builder = response_builder.clone();
                tokio::spawn(async move {
                    let io = TokioIo::new(stream);
                    let svc = service_fn(move |req| {
                        let response_builder = response_builder.clone();
                        async move { Ok::<_, std::convert::Infallible>(response_builder(&req)) }
                    });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(io, svc)
                        .await;
                });
            }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    #[test]
    fn extract_bearer_matches_and_rejects_malformed_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer abc.def-123"),
        );
        assert_eq!(extract_bearer(&headers).as_deref(), Some("abc.def-123"));

        let mut headers2 = HeaderMap::new();
        headers2.insert(AUTHORIZATION, HeaderValue::from_static("Basic abc"));
        assert_eq!(extract_bearer(&headers2), None);

        assert_eq!(extract_bearer(&HeaderMap::new()), None);
    }

    #[test]
    fn accept_includes_strict_api_media_matches_exact_and_rejects_wildcards() {
        assert!(accept_includes_strict_api_media(API_MEDIA_TYPE));
        assert!(accept_includes_strict_api_media(&format!(
            "{API_MEDIA_TYPE};q=0.9"
        )));
        assert!(accept_includes_strict_api_media(&format!(
            "text/plain, {API_MEDIA_TYPE}"
        )));
        assert!(!accept_includes_strict_api_media("application/json"));
        assert!(!accept_includes_strict_api_media("*/*"));
        assert!(!accept_includes_strict_api_media(""));
    }

    #[test]
    fn maybe_single_tenant_redirect_substitutes_only_the_first_occurrence() {
        let cfg = McpHandlerConfig {
            single_tenant_name: Some("bar".to_string()),
            ..fast_cfg()
        };
        let redirect =
            maybe_single_tenant_redirect(&cfg, "foo", "/agent-mcp/__dashboard/foo/tasks/foo", None)
                .unwrap();
        assert_eq!(redirect.status, 302);
        let location = redirect
            .headers
            .iter()
            .find(|(k, _)| k == "Location")
            .unwrap();
        assert_eq!(location.1, "/agent-mcp/__dashboard/bar/tasks/foo");
    }

    #[test]
    fn maybe_single_tenant_redirect_is_none_when_disabled_or_already_matching() {
        assert!(maybe_single_tenant_redirect(&fast_cfg(), "foo", "/x/foo", None).is_none());
        let cfg = McpHandlerConfig {
            single_tenant_name: Some("foo".to_string()),
            ..fast_cfg()
        };
        assert!(maybe_single_tenant_redirect(&cfg, "foo", "/x/foo", None).is_none());
    }

    #[tokio::test]
    async fn backend_mcp_handler_rejects_a_request_with_no_bearer_uniformly() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("proj-a", "/agent-mcp/proj-a/mcp");
        req.headers = HeaderMap::new(); // no Authorization at all

        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            req,
        )
        .await;
        assert_eq!(resp.status, 401);
    }

    #[tokio::test]
    async fn backend_mcp_handler_floors_an_unknown_project_to_the_same_401_latency() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));
        let cfg = McpHandlerConfig {
            preauth_401_floor: Duration::from_millis(80),
            ..fast_cfg()
        };

        let t0 = Instant::now();
        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &cfg,
            test_now(),
            base_req("nope", "/agent-mcp/nope/mcp"),
        )
        .await;
        let elapsed = t0.elapsed();
        assert_eq!(resp.status, 401);
        assert!(
            elapsed >= Duration::from_millis(75),
            "an unknown project's 401 must be floored to ~the configured latency, got {elapsed:?}"
        );
    }

    #[tokio::test]
    async fn backend_mcp_handler_rejects_a_disallowed_method() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("proj-a", "/agent-mcp/proj-a/mcp");
        req.method = Method::PUT;

        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            req,
        )
        .await;
        assert_eq!(resp.status, 405);
    }

    #[tokio::test]
    async fn backend_mcp_handler_proxies_a_real_request_and_collapses_a_backend_401() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_backend(sock_dir.join("proj-a").join("backend.sock"), |_req| {
            Response::builder()
                .status(StatusCode::UNAUTHORIZED)
                .header("server", "uvicorn")
                .body(Full::new(Bytes::from_static(
                    b"{\"error\":\"agent_terminated\"}",
                )))
                .unwrap()
        })
        .await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            base_req("proj-a", "/agent-mcp/proj-a/mcp"),
        )
        .await;
        // SEC5: the backend's own 401 (with its own body/Server header)
        // must be collapsed into the router's canonical envelope.
        assert_eq!(resp.status, 401);
        assert!(resp
            .headers
            .iter()
            .any(|(k, _)| k.eq_ignore_ascii_case("WWW-Authenticate")));
        assert!(!resp
            .headers
            .iter()
            .any(|(k, v)| k.eq_ignore_ascii_case("server") && v == "uvicorn"));
    }

    #[tokio::test]
    async fn backend_mcp_handler_forwards_a_real_successful_response() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_backend(sock_dir.join("proj-a").join("backend.sock"), |req| {
            assert_eq!(req.uri().path(), "/mcp");
            Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from_static(b"{\"jsonrpc\":\"2.0\"}")))
                .unwrap()
        })
        .await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            base_req("proj-a", "/agent-mcp/proj-a/mcp"),
        )
        .await;
        assert_eq!(resp.status, 200);
        match resp.body {
            HandlerBody::Proxied(ProxyResponseBody::Buffered(b)) => {
                assert_eq!(b.as_ref(), b"{\"jsonrpc\":\"2.0\"}")
            }
            other => panic!("expected a buffered proxied body, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn backend_mcp_handler_floors_an_oversized_body() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));
        let cfg = McpHandlerConfig {
            mcp_max_body_bytes: 4,
            ..fast_cfg()
        };

        let mut req = base_req("proj-a", "/agent-mcp/proj-a/mcp");
        req.body = Bytes::from_static(b"way too big for the cap");

        let resp = backend_mcp_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &cfg,
            test_now(),
            req,
        )
        .await;
        assert_eq!(
            resp.status, 401,
            "an oversized body must collapse into the uniform pre-auth 401"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_requires_the_versioned_accept_header() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("proj-a", "/agent-mcp/__api/proj-a/agents");
        req.method = Method::GET;
        req.headers = HeaderMap::new(); // no Accept header at all

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            "agents",
            req,
        )
        .await;
        assert_eq!(resp.status, 406);
    }

    #[tokio::test]
    async fn backend_api_handler_exempts_the_events_stream_and_delivery_routes() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_backend(sock_dir.join("proj-a").join("backend.sock"), |_req| {
            Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::new()))
                .unwrap()
        })
        .await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("proj-a", "/agent-mcp/__api/proj-a/events");
        req.method = Method::GET;
        req.headers = HeaderMap::new();

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            "events",
            req,
        )
        .await;
        assert_eq!(
            resp.status, 200,
            "the events stream must be exempt from the Accept-header gate"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_returns_a_real_404_for_an_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("nope", "/agent-mcp/__api/nope/agents");
        req.method = Method::GET;
        req.headers.insert(
            hyper::header::ACCEPT,
            HeaderValue::from_static(API_MEDIA_TYPE),
        );

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            "agents",
            req,
        )
        .await;
        assert_eq!(
            resp.status, 404,
            "unlike the MCP handler, the API handler has no pre-auth floor/collapse discipline in Python either"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_forwards_a_real_response_with_the_alias_header() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_backend(sock_dir.join("proj-a").join("backend.sock"), |req| {
            assert_eq!(req.uri().path(), "/api/agents");
            let alias_header = req
                .headers()
                .get("x-agent-mcp-alias")
                .map(|v| v.to_str().unwrap().to_string())
                .unwrap_or_default();
            Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(alias_header)))
                .unwrap()
        })
        .await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        registry
            .add_alias("proj-a", "old-name", None, Some(30), test_now())
            .unwrap();
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let mut req = base_req("old-name", "/agent-mcp/__api/old-name/agents");
        req.method = Method::GET;
        req.headers.insert(
            hyper::header::ACCEPT,
            HeaderValue::from_static(API_MEDIA_TYPE),
        );

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            test_now(),
            "agents",
            req,
        )
        .await;
        assert_eq!(resp.status, 200);
        match resp.body {
            HandlerBody::Proxied(ProxyResponseBody::Buffered(b)) => {
                assert!(String::from_utf8_lossy(&b).starts_with("old-name,"))
            }
            other => panic!("expected a buffered proxied body, got {other:?}"),
        }
    }
}
