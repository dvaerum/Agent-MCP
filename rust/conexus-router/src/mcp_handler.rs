//! The router's `/mcp` + `/api/*` HTTP handler layer -- port of
//! `agent_mcp/router/app.py`'s `backend_mcp_handler`/
//! `backend_api_handler` (Phase E2 PR 9). Framework-agnostic, like
//! `proxy_core.rs`: no axum types here, so app-wiring (PR 23) converts
//! this module's plain Rust request/response shapes into real axum
//! extractors/responses -- these two handler functions are the first
//! REAL callers of `proxy_core`/`orchestrator`/`project_registry`.
//!
//! **Scope**: [`backend_mcp_handler`] stays BEARER-authenticated only,
//! matching PR 8's own decision -- a caller with no bearer at all gets
//! the SAME uniform 401 Python's own no-credential branch returns,
//! with no cookie fallback attempted (that path's own dependencies
//! would need the SAME re-derivation-per-body-read discipline Python's
//! `inject_header_resolver` closure exists for; `/mcp`'s body is a
//! genuine live upstream hold for `GET /mcp`, unlike `/api`'s, so this
//! crate's simpler "resolve once, pass a signed value" shape from
//! [`backend_api_handler`] below doesn't carry over cleanly -- left
//! for a later PR rather than forced in here).
//!
//! [`backend_api_handler`] DOES apply a cookie-authenticated operator
//! session when no bearer is present -- it takes the already-resolved
//! `(operator_id, role)` as its `cookie_role` parameter rather than
//! resolving it itself (see that function's own doc for why: it stays
//! DB-free, its caller -- `proxy_routes.rs` -- resolves the cookie via
//! `crate::cookie_forwarding` under a short-lived DB lock first). This
//! closes the gap that left every cookie-authenticated dashboard
//! request (`all-data`/`events`) 401ing once a project's backend is
//! the Rust `conexus-backend` (which, unlike the old Python backend,
//! has no raw-cookie admission door of its own; see
//! `conexus-backend::rest_principal`'s own module doc for why that
//! door was deliberately dropped).
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

use conexus_auth::forwarding_header::{self, ForwardedRole};

use crate::orchestrator::ensure::{self, EnsureConfig, EnsureError};
use crate::orchestrator::primitives::ensure_forwarding_hmac_key;
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

/// The ONE conversion from this crate's framework-agnostic vocabulary
/// into a real axum `Response` -- every PR23 app-wiring step's
/// handlers/middleware return a `HandlerResponse` and let this `impl`
/// do the framework-specific work, rather than each call site
/// re-deriving it. `Proxied(Streaming(..))` streams straight through
/// (`axum::body::Body::from_stream`, no full-response buffering) --
/// the one case this crate's decision-function layer could never
/// build directly, since `HandlerResponse` has no axum types in its
/// own definition.
impl axum::response::IntoResponse for HandlerResponse {
    fn into_response(self) -> axum::response::Response {
        let mut builder = axum::http::Response::builder().status(
            axum::http::StatusCode::from_u16(self.status)
                .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR),
        );
        for (name, value) in &self.headers {
            if let (Ok(name), Ok(value)) = (
                axum::http::HeaderName::try_from(name.as_str()),
                axum::http::HeaderValue::try_from(value.as_str()),
            ) {
                builder = builder.header(name, value);
            }
        }
        let body = match self.body {
            HandlerBody::Empty => axum::body::Body::empty(),
            HandlerBody::Text(s) => axum::body::Body::from(s),
            HandlerBody::Json(v) => {
                builder = builder.header(
                    axum::http::header::CONTENT_TYPE,
                    axum::http::HeaderValue::from_static("application/json"),
                );
                axum::body::Body::from(v.to_string())
            }
            HandlerBody::Proxied(proxy_core::ProxyResponseBody::Buffered(bytes)) => {
                axum::body::Body::from(bytes)
            }
            HandlerBody::Proxied(proxy_core::ProxyResponseBody::Streaming(stream)) => {
                axum::body::Body::from_stream(stream)
            }
        };
        builder
            .body(body)
            .unwrap_or_else(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response())
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

/// Map an [`EnsureError`] to `(status, message, retry_after)` -- port
/// of the status/reason pairs `_ensure`'s own raised `web.HTTP*`
/// exceptions carry (see `orchestrator::ensure`'s own doc for the
/// underlying cases). `retry_after` is `Some` only for
/// [`EnsureError::GaveUp`] (SC-R7-1 livelock fix): a caller hitting
/// the give-up state gets a concrete, real wait hint instead of
/// hammering the router again immediately.
fn ensure_error_status(e: &EnsureError) -> (u16, String, Option<Duration>) {
    match e {
        EnsureError::UnknownProject => (404, "unknown project".to_string(), None),
        EnsureError::Cooldown(reason) => (504, reason.message().to_string(), None),
        EnsureError::Failed(EnsureFailureReason::SystemctlFailed) => (
            500,
            EnsureFailureReason::SystemctlFailed.message().to_string(),
            None,
        ),
        EnsureError::Failed(EnsureFailureReason::SocketTimeout) => (
            504,
            EnsureFailureReason::SocketTimeout.message().to_string(),
            None,
        ),
        EnsureError::GaveUp { retry_after } => (
            503,
            "backend repeatedly failed to become ready; try again shortly".to_string(),
            Some(*retry_after),
        ),
        EnsureError::Registry(reg) => (500, reg.to_string(), None),
        EnsureError::UnitName(u) => (500, u.to_string(), None),
        EnsureError::Io(io) => (500, io.to_string(), None),
    }
}

/// Map an [`EnsureError`] straight to a [`HandlerResponse`] -- shared
/// by [`proxy_error_response`]'s `ProxyError::Ensure` arm and
/// [`backend_api_handler`]'s own pre-proxy `ensure()` call (the
/// cookie-forwarding bridge's F015 v5 step), so the SAME real failure
/// produces the byte-identical response regardless of which of those
/// two call sites happens to hit it first.
fn ensure_error_response(e: &EnsureError) -> HandlerResponse {
    let (status, message, retry_after) = ensure_error_status(e);
    // Port of `too_many_requests_response`'s own ceil-to-whole-seconds
    // convention (rate_limit.rs) -- an HTTP `Retry-After` is defined in
    // whole seconds, and rounding UP means a caller never retries a
    // hair too early.
    let headers = match retry_after {
        Some(d) => vec![(
            "Retry-After".to_string(),
            (d.as_secs_f64().ceil().max(1.0) as u64).to_string(),
        )],
        None => vec![],
    };
    HandlerResponse {
        status,
        headers,
        body: HandlerBody::Text(message),
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
        ProxyError::Ensure(inner) => ensure_error_response(&inner),
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
        None,
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
/// `backend_api_handler`, PLUS the cookie-authenticated forwarding
/// bridge Python's OWN `backend_api_handler` never had either (see
/// this module's own doc and `crate::cookie_forwarding`'s doc for why
/// that's a deliberate improvement over Python's parity target, not a
/// divergence bug): when the caller has no bearer, a session cookie
/// resolving to a real project-member operator gets a freshly-signed
/// forwarding header minted and attached, closing the gap that left
/// every cookie-authenticated dashboard request 401ing against the
/// Rust `conexus-backend`. A bearer-authenticated (or fully
/// unauthenticated) request's behavior is UNCHANGED.
///
/// `cookie_role` is the caller's ALREADY-RESOLVED `(operator_id, role)`
/// pair (`crate::cookie_forwarding::resolve_cookie_project_role`,
/// called by the axum wrapper -- `proxy_routes.rs` -- under a SHORT
/// `RouterState.conn` lock, dropped before this function ever runs).
/// This function stays DB-free by design (matching every other
/// framework-agnostic handler in this module): a `rusqlite::Connection`
/// borrowed for this whole async call would tie `RouterState.conn`'s
/// single shared lock to the full `ensure()`/UDS-proxy round-trip below
/// (`lifecycle_rest.rs::delete_project_handler`'s own scoped-`{}`-block
/// precedent is exactly the pattern this signature avoids needing).
/// Still enforced HERE, not just trusted from the caller: `cookie_role`
/// is only ever applied when [`extract_bearer`] finds nothing on THIS
/// call, so a future caller that (incorrectly) supplies both a bearer
/// and a resolved `cookie_role` can never have the cookie identity win.
#[allow(clippy::too_many_arguments)]
pub async fn backend_api_handler(
    store: &RuntimeStore,
    stream_caps: &Arc<StreamCapRegistry>,
    registry: &ProjectRegistry,
    sock_dir: &std::path::Path,
    ensure_cfg: &EnsureConfig,
    cfg: &McpHandlerConfig,
    cookie_role: Option<(String, ForwardedRole)>,
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

    // Cookie-forwarding bridge: only applied when the caller carries NO
    // bearer at all -- a bearer, even an invalid one, is a stronger
    // credential this crate never downgrades away from (re-checked
    // HERE, not just trusted from `cookie_role`'s caller -- see this
    // function's own doc).
    let mut forwarding_header_value: Option<String> = None;
    if let (None, Some((operator_id, role))) = (extract_bearer(&req.headers), cookie_role) {
        // F015 v5 parity: ensure the backend is actually spawned BEFORE
        // reading its HMAC key off disk -- the key is written by the
        // systemd unit's own `ExecStartPre`, which only runs once
        // `ensure()` triggers a `systemctl start`. A cold backend with
        // no prior bearer traffic would otherwise 401 every
        // cookie-authenticated dashboard request in a tight loop (see
        // `agent_mcp/router/app.py::_forwarding_header_from_cookie`'s
        // own F015 v5 note).
        match ensure::ensure(store, registry, sock_dir, &real_name, "backend", ensure_cfg).await {
            Ok(_) => {
                if let Ok(Some(key)) = ensure_forwarding_hmac_key(store, sock_dir, &real_name) {
                    // A FRESH clock read here, not the `now` this function
                    // received at entry: `ensure()` above can legitimately
                    // run for many real seconds on a cold start (up to the
                    // full `boot_grace` window) -- signing against the
                    // stale entry-time `now` could hand the backend a
                    // header whose `DEFAULT_TTL_SEC` had ALREADY elapsed
                    // before it ever left this process. Matches Python's
                    // own `_fh.sign(...)` call, which reads `time.time()`
                    // internally at this exact point (AFTER its own
                    // `await _ensure(...)`), and this crate's established
                    // "a genuinely time-spanning function reads the clock
                    // at multiple points" precedent (see
                    // `orchestrator::ensure`'s own module doc).
                    let now_unix = Utc::now().timestamp().max(0) as u64;
                    forwarding_header_value = Some(forwarding_header::sign(
                        &operator_id,
                        role,
                        &key,
                        now_unix,
                        forwarding_header::DEFAULT_TTL_SEC,
                    ));
                }
                // else: the HMAC key still isn't on disk even after
                // ensure() -- a deployment-side bug (the unit's
                // ExecStartPre didn't run/write it), not this
                // resolver's to mask. Fall through with no forwarding
                // header; the backend's own `rest_gate` produces the
                // correct 401.
            }
            Err(e) => {
                // A real spawn failure (bad unit, timeout, or the
                // repeated-failure `GaveUp` case) -- the SAME failure
                // `proxy_to_backend` would hit a few lines down if this
                // fell through silently. Surface it now with the
                // identical mapping, rather than a misleading 401.
                return ensure_error_response(&e);
            }
        }
    }

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
        forwarding_header_value.as_deref(),
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
            max_restart_attempts: 5,
            giveup_cooldown: Duration::from_secs(600),
        }
    }

    #[test]
    fn proxy_error_response_maps_ensure_gave_up_to_a_503_with_a_real_retry_after() {
        // SC-R7-1 livelock fix: a caller hitting the give-up state
        // must see a distinct, actionable status -- not another 504
        // indistinguishable from an ordinary socket-poll timeout --
        // with a real `Retry-After` computed from the remaining
        // give-up cooldown, ceiled up to whole seconds.
        let response = proxy_error_response(ProxyError::Ensure(EnsureError::GaveUp {
            retry_after: Duration::from_millis(2500),
        }));
        assert_eq!(response.status, 503);
        assert_eq!(
            response.headers,
            vec![("Retry-After".to_string(), "3".to_string())]
        );
    }

    #[test]
    fn proxy_error_response_maps_socket_timeout_to_a_generic_504_reason() {
        // SC-R9-1: the socket-poll-timeout branch of `_ensure` must
        // not leak the raw unit name / absolute socket path into the
        // client-facing reason -- same hygiene the systemctl-failure
        // sibling (SC-R8-2) already gets from `EnsureFailureReason::
        // message()`'s own fixed strings.
        let response = proxy_error_response(ProxyError::Ensure(EnsureError::Failed(
            EnsureFailureReason::SocketTimeout,
        )));
        assert_eq!(response.status, 504);
        let HandlerBody::Text(message) = response.body else {
            panic!("expected a text body");
        };
        assert_eq!(message, "backend not ready");
        assert!(!message.contains("agent-mcp@"), "leaked unit: {message:?}");
        assert!(
            !message.contains(".sock"),
            "leaked socket file: {message:?}"
        );
        assert!(!message.contains('/'), "leaked a path: {message:?}");
        assert!(
            !message.contains("did not create"),
            "leaked internals: {message:?}"
        );
    }

    #[test]
    fn proxy_error_response_maps_systemctl_failed_to_a_generic_500_reason() {
        // SC-R8-2's sibling assertion, kept alongside the SocketTimeout
        // case above so both `EnsureFailureReason` variants have an
        // explicit regression test at this mapping layer.
        let response = proxy_error_response(ProxyError::Ensure(EnsureError::Failed(
            EnsureFailureReason::SystemctlFailed,
        )));
        assert_eq!(response.status, 500);
        let HandlerBody::Text(message) = response.body else {
            panic!("expected a text body");
        };
        assert_eq!(message, "backend failed to start");
        assert!(!message.contains("agent-mcp@"), "leaked unit: {message:?}");
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
            None,
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
            None,
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
            None,
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
            None,
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

    // ── Cookie-forwarding bridge (the fix this module's own doc
    // describes). `backend_api_handler` takes an already-resolved
    // `cookie_role` -- these tests exercise ITS handling of that input
    // directly; `crate::cookie_forwarding`'s own tests cover the DB
    // resolution that produces it. ─────────────────────────────────

    async fn spawn_echo_forwarding_header_backend(sock_path: std::path::PathBuf) {
        spawn_backend(sock_path, |req| {
            let header = req
                .headers()
                .get("x-agent-mcp-forwarded-operator")
                .map(|v| v.to_str().unwrap().to_string())
                .unwrap_or_default();
            Response::builder()
                .status(StatusCode::OK)
                .body(Full::new(Bytes::from(header)))
                .unwrap()
        })
        .await;
    }

    fn cookie_bridge_req(no_bearer: bool) -> HandlerRequest {
        let mut req = base_req("proj-a", "/agent-mcp/__api/proj-a/all-data");
        req.method = Method::GET;
        req.headers.insert(
            hyper::header::ACCEPT,
            HeaderValue::from_static(API_MEDIA_TYPE),
        );
        if no_bearer {
            req.headers.remove(AUTHORIZATION);
        }
        req
    }

    #[tokio::test]
    async fn backend_api_handler_mints_a_valid_forwarding_header_when_cookie_role_is_resolved() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let key = b"a-real-32-byte-test-hmac-key!!!!".to_vec();
        std::fs::write(sock_dir.join("proj-a").join("forwarding_hmac"), &key).unwrap();
        spawn_echo_forwarding_header_backend(sock_dir.join("proj-a").join("backend.sock")).await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            Some(("op1".to_string(), ForwardedRole::Operator)),
            test_now(),
            "all-data",
            cookie_bridge_req(true),
        )
        .await;
        assert_eq!(resp.status, 200);
        let HandlerBody::Proxied(ProxyResponseBody::Buffered(body)) = resp.body else {
            panic!("expected a buffered proxied body");
        };
        let header_value = String::from_utf8_lossy(&body).to_string();
        assert!(
            !header_value.is_empty(),
            "a resolved cookie_role must mint a forwarding header"
        );
        // `sign()` is now called against a FRESH `Utc::now()` read
        // inside `backend_api_handler` itself (see its own comment),
        // not the fixed `test_now()` this test's OTHER params use --
        // verify against the real wall clock too.
        let now_unix = Utc::now().timestamp() as u64;
        let verified = forwarding_header::verify(
            &header_value,
            &key,
            now_unix,
            forwarding_header::DEFAULT_REPLAY_WINDOW_SEC,
        );
        assert_eq!(
            verified,
            Some(("op1".to_string(), ForwardedRole::Operator)),
            "the minted header must verify with the SAME scheme conexus-backend's \
             rest_principal uses, and carry the resolved operator/role"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_mints_a_viewer_role_header_not_operator() {
        // SEC-1 parity: a viewer-tier `cookie_role` must never be
        // upgraded to operator.
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let key = b"a-real-32-byte-test-hmac-key!!!!".to_vec();
        std::fs::write(sock_dir.join("proj-a").join("forwarding_hmac"), &key).unwrap();
        spawn_echo_forwarding_header_backend(sock_dir.join("proj-a").join("backend.sock")).await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            Some(("op2".to_string(), ForwardedRole::Viewer)),
            test_now(),
            "all-data",
            cookie_bridge_req(true),
        )
        .await;
        let HandlerBody::Proxied(ProxyResponseBody::Buffered(body)) = resp.body else {
            panic!("expected a buffered proxied body");
        };
        let header_value = String::from_utf8_lossy(&body).to_string();
        // `sign()` is now called against a FRESH `Utc::now()` read
        // inside `backend_api_handler` itself (see its own comment),
        // not the fixed `test_now()` this test's OTHER params use --
        // verify against the real wall clock too.
        let now_unix = Utc::now().timestamp() as u64;
        let verified = forwarding_header::verify(
            &header_value,
            &key,
            now_unix,
            forwarding_header::DEFAULT_REPLAY_WINDOW_SEC,
        );
        assert_eq!(verified, Some(("op2".to_string(), ForwardedRole::Viewer)));
    }

    #[tokio::test]
    async fn backend_api_handler_mints_no_header_when_cookie_role_is_none() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_echo_forwarding_header_backend(sock_dir.join("proj-a").join("backend.sock")).await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            None, // no cookie, no session, or not a project member
            test_now(),
            "all-data",
            cookie_bridge_req(true),
        )
        .await;
        assert_eq!(resp.status, 200);
        let HandlerBody::Proxied(ProxyResponseBody::Buffered(body)) = resp.body else {
            panic!("expected a buffered proxied body");
        };
        assert_eq!(
            body.as_ref(),
            b"",
            "no resolved cookie_role must never mint a forwarding header"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_ignores_a_resolved_cookie_role_when_a_bearer_is_present() {
        // A bearer is a stronger credential this crate never downgrades
        // away from -- even a resolved `cookie_role` must be ignored
        // once a bearer is present (task requirement: bearer-
        // authenticated behavior is unaffected by this change). This
        // is `backend_api_handler`'s own defense-in-depth re-check, not
        // just caller discipline -- see its doc.
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        spawn_echo_forwarding_header_backend(sock_dir.join("proj-a").join("backend.sock")).await;

        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        // bearer_headers() is already set by base_req (no_bearer=false).
        let req = cookie_bridge_req(false);

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            Some(("op1".to_string(), ForwardedRole::Operator)),
            test_now(),
            "all-data",
            req,
        )
        .await;
        assert_eq!(resp.status, 200);
        let HandlerBody::Proxied(ProxyResponseBody::Buffered(body)) = resp.body else {
            panic!("expected a buffered proxied body");
        };
        assert_eq!(
            body.as_ref(),
            b"",
            "a bearer-carrying request must never mint a cookie-derived forwarding header"
        );
    }

    #[tokio::test]
    async fn backend_api_handler_surfaces_a_real_ensure_failure_for_a_cookie_authenticated_caller()
    {
        // No backend spawned at all -- `ensure()`'s own socket-poll
        // timeout must surface as the SAME real error status
        // `proxy_to_backend` would produce a few lines down, not a
        // misleading 401 (matches `proxy_core.rs`'s own
        // `proxy_to_backend_reports_backend_unavailable_for_a_missing_socket`
        // precedent for the equivalent bearer-path failure).
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let stream_caps = Arc::new(StreamCapRegistry::new(4, 64));

        let resp = backend_api_handler(
            &store,
            &stream_caps,
            &registry,
            &sock_dir,
            &fast_ensure_cfg(),
            &fast_cfg(),
            Some(("op1".to_string(), ForwardedRole::Operator)),
            test_now(),
            "all-data",
            cookie_bridge_req(true),
        )
        .await;
        assert_eq!(
            resp.status, 504,
            "a real ensure() failure must surface as its own real status, not a misleading 401"
        );
    }
}
