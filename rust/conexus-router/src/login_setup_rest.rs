//! Real axum routes for the login / logout / setup-wizard HTML
//! surface -- port target: `agent_mcp/router/login.py` (597 LOC) +
//! `agent_mcp/router/setup_wizard.py` (256 LOC)'s HANDLER layer
//! (Phase E2 PR23 step 4, `conexus-router-login-setup-templates`).
//! The decision logic (`login.rs`) and the rendering layer
//! (`templates.rs`, minijinja) already exist; this module is pure
//! wiring, matching every other PR23 step's own precedent.
//!
//! Registered on `admin_router` (session-gated), same as every other
//! dashboard route -- `path_policy::UNAUTH_PREFIXES` already lists
//! `/agent-mcp/login`/`/agent-mcp/logout`/`/agent-mcp/setup`, so
//! `session_gate_layer` resolves these to `PassThrough` and never
//! blocks them; these handlers do their OWN independent cookie
//! resolution (`login::resolve_current_user`) rather than relying on
//! a gate-supplied `GateIdentity`.
//!
//! `empty_users_redirect_layer` (already wired, `middleware.rs`) is
//! what bounces every OTHER `/agent-mcp/...` request to `/setup`
//! while the users table is empty; `/agent-mcp/setup` itself is
//! `path_policy::REDIRECT_EXEMPT_PREFIXES`-listed so it doesn't loop.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, Form, OriginalUri, RawQuery, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use chrono::Utc;
use serde::Deserialize;

use crate::identity;
use crate::login::{self, LoginAttemptOutcome, SetupError, SetupGetOutcome, SetupPostOutcome};
use crate::middleware::{is_request_trusted, peer_info};
use crate::mount;
use crate::sso;
use crate::state::RouterState;
use crate::templates::{self, LoginPageContext, SetupPageContext};

fn header_str<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

/// Extract one query-string parameter by hand -- port of Python's
/// `request.rel_url.query.get("next")`. Deliberately not
/// `axum::extract::Query` (which 400s the whole request on a
/// malformed query string): this endpoint is unauthenticated, and a
/// broken `next=` should degrade to "no next", never fail the login
/// page outright. Reuses the already-workspace-resolved
/// `percent-encoding` crate rather than adding `serde_urlencoded`/
/// `url` as a fresh dependency for one field.
fn query_param(raw_query: Option<&str>, name: &str) -> Option<String> {
    let raw = raw_query?;
    raw.split('&').find_map(|pair| {
        let (k, v) = pair.split_once('=')?;
        (k == name).then(|| {
            percent_encoding::percent_decode_str(v)
                .decode_utf8_lossy()
                .into_owned()
        })
    })
}

/// Per-request mount/trust resolution -- the SAME `peer_info`/
/// `is_request_trusted`/`mount::canonical_path` composition
/// `dashboard_handlers.rs`/`middleware.rs` already establish, needed
/// here fresh because these routes sit outside the session gate's own
/// principal resolution (see this module's own doc).
struct RequestMount {
    canonical_path: String,
    is_trusted: bool,
    forwarded_prefix: Option<String>,
    forwarded_proto: Option<String>,
    forwarded_host: Option<String>,
    host: String,
}

impl RequestMount {
    fn resolve(state: &RouterState, addr: SocketAddr, raw_path: &str, headers: &HeaderMap) -> Self {
        let peer = peer_info(addr);
        RequestMount {
            canonical_path: mount::canonical_path(raw_path),
            is_trusted: is_request_trusted(state, &peer),
            forwarded_prefix: header_str(headers, "x-forwarded-prefix").map(str::to_string),
            forwarded_proto: header_str(headers, "x-forwarded-proto").map(str::to_string),
            forwarded_host: header_str(headers, "x-forwarded-host").map(str::to_string),
            host: header_str(headers, "host").unwrap_or_default().to_string(),
        }
    }

    fn external_path(&self, suffix: &str) -> String {
        mount::external_path(
            &self.canonical_path,
            self.is_trusted,
            self.forwarded_prefix.as_deref(),
            suffix,
        )
    }

    /// Port of `login.py::_external_origin`. This binary terminates no
    /// TLS itself (matches `security_headers_layer`'s own established
    /// convention) -- the real transport scheme is always `"http"`; a
    /// trusted proxy's `X-Forwarded-Proto` is what actually flips it.
    fn external_origin(&self) -> String {
        mount::external_origin(
            "http",
            &self.host,
            self.is_trusted,
            self.forwarded_proto.as_deref(),
            self.forwarded_host.as_deref(),
        )
    }
}

fn html_response(status: StatusCode, body: String) -> Response {
    (
        status,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body,
    )
        .into_response()
}

fn see_other(location: &str) -> Response {
    (StatusCode::SEE_OTHER, [(header::LOCATION, location)]).into_response()
}

fn see_other_with_cookie(location: &str, cookie: &login::SessionCookie) -> Response {
    (
        StatusCode::SEE_OTHER,
        [
            (header::LOCATION, location.to_string()),
            (header::SET_COOKIE, cookie.to_header_value()),
        ],
    )
        .into_response()
}

/// Port of the bare `web.HTTPForbidden(reason="Cross-origin request rejected")`
/// `enforce_same_origin` raises -- aiohttp's own default exception
/// body for an unadorned `HTTPForbidden`.
fn forbidden_cross_origin() -> Response {
    (StatusCode::FORBIDDEN, "403: Forbidden").into_response()
}

fn internal_error() -> Response {
    (StatusCode::INTERNAL_SERVER_ERROR, "internal error").into_response()
}

/// Reads the `users` table's empty/non-empty state. `Err(())` means a
/// genuine DB error -- every handler below maps it to a 500 itself
/// via `internal_error()`, rather than this fn returning a full
/// `Response` as its error variant (clippy::result_large_err: a
/// `hyper::Response<axum::body::Body>` is >128 bytes and this error
/// carries no information beyond "something went wrong").
async fn users_table_is_empty(state: &RouterState) -> Result<bool, ()> {
    let conn = state.conn.lock().await;
    identity::users_table_is_empty(&conn).map_err(|_| ())
}

/// Port of `identity.create_user`'s internal `_list_registered_projects()`
/// call -- resolved HERE (app-wiring), not inside `login.rs`'s
/// `create_first_operator`, matching that function's own documented
/// deferral ("wiring the real `ProjectRegistry::list()` is
/// app-wiring's job, not this module's"). Empty on any registry read
/// error, matching Python's own defensive fallback ("a first-boot
/// deploy with no projects yet" must not crash the bootstrap on a
/// missing/corrupt registry file).
fn registered_project_names(state: &RouterState) -> Vec<String> {
    state
        .registry
        .list()
        .map(|rows| rows.into_iter().map(|r| r.name).collect())
        .unwrap_or_default()
}

// ── GET/POST /agent-mcp/login ───────────────────────────────────────

pub async fn login_get_handler(
    State(state): State<Arc<RouterState>>,
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    RawQuery(raw_query): RawQuery,
) -> Response {
    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);
    let now = Utc::now().to_rfc3339();
    let cookie_header = header_str(&headers, "cookie");

    let current_user = {
        let conn = state.conn.lock().await;
        match login::resolve_current_user(&conn, cookie_header, &now) {
            Ok(u) => u,
            Err(_) => return internal_error(),
        }
    };

    let next_param = query_param(raw_query.as_deref(), "next");
    if current_user.is_some() {
        let target = login::safe_next(next_param.as_deref(), &mount_ctx.external_path("/"));
        return see_other(&target);
    }

    let next_display = next_param.unwrap_or_default();
    let login_action = mount_ctx.external_path("/login");
    let sso_login_url = mount_ctx.external_path("/sso/login");
    // Port of `_resolve_sso_provider_name`: a config-load failure
    // degrades to `None` (legacy form) exactly like Python's own
    // `except Exception: return None` -- the login page must still
    // render so the operator can read the real error from the
    // journal/logs and fix the config, not 500 on every visit.
    let sso_settings = sso::load_sso_config(
        |key| std::env::var(key).ok(),
        |path| std::fs::read_to_string(path),
    )
    .ok();
    let sso_provider_name = sso::resolve_sso_provider_name(sso_settings.as_ref());
    let html = templates::render_login(&LoginPageContext {
        error: None,
        username: "",
        next: &next_display,
        sso_provider_name: sso_provider_name.as_deref(),
        login_action: &login_action,
        sso_login_url: &sso_login_url,
    });
    html_response(StatusCode::OK, html)
}

#[derive(Deserialize, Default)]
pub struct LoginFormBody {
    #[serde(default)]
    username: String,
    #[serde(default)]
    password: String,
}

pub async fn login_post_handler(
    State(state): State<Arc<RouterState>>,
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    RawQuery(raw_query): RawQuery,
    form: Result<Form<LoginFormBody>, axum::extract::rejection::FormRejection>,
) -> Response {
    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);

    // Login-CSRF guard (R9-F1): this POST mints a session cookie, so
    // `SameSite=Lax` gives it no protection. Reject a cross-site
    // request before touching credentials or the form body at all.
    let origin = header_str(&headers, "origin");
    let sec_fetch_site = header_str(&headers, "sec-fetch-site");
    if login::enforce_same_origin(origin, sec_fetch_site, &mount_ctx.external_origin()).is_err() {
        return forbidden_cross_origin();
    }

    let next_url = query_param(raw_query.as_deref(), "next").unwrap_or_default();
    let login_action = mount_ctx.external_path("/login");
    let sso_login_url = mount_ctx.external_path("/sso/login");

    let Ok(Form(body)) = form else {
        // A malformed form body (bad content-type, invalid urlencoding)
        // must not 500 an unauthenticated attacker's own oracle
        // (PF-R21-1) -- fold it into the same invalid-credentials
        // re-render Python's own `except (ValueError, UnicodeDecodeError)`
        // branch produces.
        let html = templates::render_login(&LoginPageContext {
            error: Some("Invalid username or password."),
            username: "",
            next: &next_url,
            sso_provider_name: None,
            login_action: &login_action,
            sso_login_url: &sso_login_url,
        });
        return html_response(StatusCode::UNAUTHORIZED, html);
    };

    let username = body.username.trim().to_string();
    let password = body.password;

    let render_invalid = |username: &str| {
        templates::render_login(&LoginPageContext {
            error: Some("Invalid username or password."),
            username,
            next: &next_url,
            sso_provider_name: None,
            login_action: &login_action,
            sso_login_url: &sso_login_url,
        })
    };

    if username.is_empty() || password.is_empty() {
        return html_response(StatusCode::UNAUTHORIZED, render_invalid(&username));
    }

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let outcome = {
        let conn = state.conn.lock().await;
        match login::attempt_login(&conn, &username, &password) {
            Ok(o) => o,
            Err(_) => return internal_error(),
        }
    };
    match outcome {
        LoginAttemptOutcome::InvalidCredentials => {
            html_response(StatusCode::UNAUTHORIZED, render_invalid(&username))
        }
        LoginAttemptOutcome::Success(user) => {
            let session_id = {
                let conn = state.conn.lock().await;
                let expires = (now
                    + chrono::Duration::days(identity::DEFAULT_SESSION_LIFETIME_DAYS))
                .to_rfc3339();
                let sid = match identity::create_session(&conn, &user.user_id, &now_str, &expires) {
                    Ok(s) => s,
                    Err(_) => return internal_error(),
                };
                if identity::touch_last_login(&conn, &user.user_id, &now_str).is_err() {
                    return internal_error();
                }
                sid
            };
            let target = login::safe_next(Some(&next_url), &mount_ctx.external_path("/"));
            let secure = login::cookie_secure_flag(
                require_secure_cookies_env(),
                mount_ctx
                    .is_trusted
                    .then_some(mount_ctx.forwarded_proto.as_deref())
                    .flatten(),
                "http",
            );
            let cookie =
                login::set_session_cookie(&session_id, &mount_ctx.external_path(""), secure);
            see_other_with_cookie(&target, &cookie)
        }
    }
}

/// Port of `_require_secure_cookies`. Reuses `rate_limit::env_truthy`
/// (already the crate's one canonical truthy-string parser) rather
/// than hand-rolling a second one.
fn require_secure_cookies_env() -> bool {
    crate::rate_limit::env_truthy(
        std::env::var("AGENT_MCP_REQUIRE_SECURE_COOKIES")
            .ok()
            .as_deref(),
    )
}

// ── POST/GET /agent-mcp/logout ──────────────────────────────────────

pub async fn logout_post_handler(
    State(state): State<Arc<RouterState>>,
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Response {
    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);
    let cookie_header = header_str(&headers, "cookie");
    let session_id =
        cookie_header.and_then(|h| login::parse_cookie_header(h, login::SESSION_COOKIE_NAME));

    if let Some(session_id) = session_id.filter(|s| !s.is_empty()) {
        let conn = state.conn.lock().await;
        if identity::delete_session(&conn, &session_id).is_err() {
            return internal_error();
        }
    }

    let secure = login::cookie_secure_flag(
        require_secure_cookies_env(),
        mount_ctx
            .is_trusted
            .then_some(mount_ctx.forwarded_proto.as_deref())
            .flatten(),
        "http",
    );
    let cookie = login::clear_session_cookie(&mount_ctx.external_path(""), secure);
    // Python hardcodes this redirect target literally (`"/agent-mcp/login"`),
    // never mount-aware -- the SAME preserved, deliberate quirk
    // documented for step 9's `redirect_to_app_index`/
    // `redirect_to_app_page` closures (a root-mounted alias still
    // bounces to the `/agent-mcp/`-prefixed URL). Ported identically,
    // not "fixed" into a smarter same-mount redirect.
    see_other_with_cookie("/agent-mcp/login", &cookie)
}

/// `GET /agent-mcp/logout` -- port of `logout_get_handler`. Logout
/// itself stays POST-only (CSRF: a cross-site image/link tag must not
/// force a session drop); a GET just bounces to `/login`, matching
/// Python's own hardcoded, non-mount-aware target exactly.
pub async fn logout_get_handler() -> Response {
    see_other("/agent-mcp/login")
}

// ── GET/POST /agent-mcp/setup ────────────────────────────────────────

pub async fn setup_get_handler(State(state): State<Arc<RouterState>>) -> Response {
    let empty = match users_table_is_empty(&state).await {
        Ok(e) => e,
        Err(()) => return internal_error(),
    };
    match login::setup_get_outcome(empty) {
        SetupGetOutcome::RedirectToLogin => see_other("/agent-mcp/login"),
        SetupGetOutcome::RenderForm => {
            let html = templates::render_setup(&SetupPageContext {
                error: None,
                username: "",
                email: "",
            });
            html_response(StatusCode::OK, html)
        }
    }
}

#[derive(Deserialize, Default)]
pub struct SetupFormBody {
    #[serde(default)]
    username: String,
    #[serde(default)]
    password: String,
    #[serde(default)]
    password_confirm: String,
    #[serde(default)]
    email: String,
}

fn setup_error_message(err: &SetupError) -> String {
    match err {
        SetupError::EmptyUsername => "Username is required.".to_string(),
        SetupError::EmptyPassword => "Password is required.".to_string(),
        SetupError::PasswordMismatch => "Passwords do not match.".to_string(),
        SetupError::WeakPassword(msg) => msg.clone(),
        // Both remaining variants are handled by the caller before
        // this fn is ever reached (`UsernameAlreadyExists` folds into
        // `AlreadySetUp`; `Db` is a genuine 500) -- kept exhaustive so
        // a future `SetupError` variant is a compile-time-visible gap
        // here, not a silent fallthrough.
        SetupError::UsernameAlreadyExists => "Username is required.".to_string(),
        SetupError::Db(_) => "internal error".to_string(),
    }
}

pub async fn setup_post_handler(
    State(state): State<Arc<RouterState>>,
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    form: Result<Form<SetupFormBody>, axum::extract::rejection::FormRejection>,
) -> Response {
    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);

    let origin = header_str(&headers, "origin");
    let sec_fetch_site = header_str(&headers, "sec-fetch-site");
    if login::enforce_same_origin(origin, sec_fetch_site, &mount_ctx.external_origin()).is_err() {
        return forbidden_cross_origin();
    }

    let empty = match users_table_is_empty(&state).await {
        Ok(e) => e,
        Err(()) => return internal_error(),
    };
    if !empty {
        // A POST after the wizard's already completed -- most likely a
        // back-button replay. Bounce to /login rather than a 409.
        return see_other("/agent-mcp/login");
    }

    let Ok(Form(body)) = form else {
        let html = templates::render_setup(&SetupPageContext {
            error: Some("Invalid form submission."),
            username: "",
            email: "",
        });
        return html_response(StatusCode::BAD_REQUEST, html);
    };

    let username = body.username.trim().to_string();
    let password = body.password;
    let password_confirm = body.password_confirm;
    let email_trimmed = body.email.trim().to_string();
    let email = (!email_trimmed.is_empty()).then_some(email_trimmed.as_str());

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let registered_projects = registered_project_names(&state);

    let outcome = {
        let mut conn = state.conn.lock().await;
        login::attempt_setup(
            &mut conn,
            true,
            &username,
            &password,
            &password_confirm,
            email,
            &registered_projects,
            &now_str,
        )
    };

    match outcome {
        SetupPostOutcome::AlreadySetUp => see_other("/agent-mcp/login"),
        SetupPostOutcome::Invalid(err) => {
            let message = setup_error_message(&err);
            let html = templates::render_setup(&SetupPageContext {
                error: Some(&message),
                username: &username,
                email: email.unwrap_or(""),
            });
            html_response(StatusCode::BAD_REQUEST, html)
        }
        SetupPostOutcome::Created(user_id) => {
            let session_id = {
                let conn = state.conn.lock().await;
                let expires = (now
                    + chrono::Duration::days(identity::DEFAULT_SESSION_LIFETIME_DAYS))
                .to_rfc3339();
                let sid = match identity::create_session(&conn, &user_id, &now_str, &expires) {
                    Ok(s) => s,
                    Err(_) => return internal_error(),
                };
                if identity::touch_last_login(&conn, &user_id, &now_str).is_err() {
                    return internal_error();
                }
                sid
            };
            let secure = login::cookie_secure_flag(
                require_secure_cookies_env(),
                mount_ctx
                    .is_trusted
                    .then_some(mount_ctx.forwarded_proto.as_deref())
                    .flatten(),
                "http",
            );
            let cookie =
                login::set_session_cookie(&session_id, &mount_ctx.external_path(""), secure);
            // Port of `setup_post_handler`'s own hardcoded, non-mount-
            // aware redirect target -- same preserved quirk as
            // logout's.
            see_other_with_cookie("/agent-mcp/", &cookie)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- query_param ----------------------------------------------------

    #[test]
    fn query_param_finds_the_named_field() {
        assert_eq!(
            query_param(Some("next=/app/&foo=bar"), "next"),
            Some("/app/".to_string())
        );
    }

    #[test]
    fn query_param_returns_none_when_absent_or_query_missing() {
        assert_eq!(query_param(Some("foo=bar"), "next"), None);
        assert_eq!(query_param(None, "next"), None);
    }

    #[test]
    fn query_param_percent_decodes_the_value() {
        assert_eq!(
            query_param(Some("next=%2Fapp%2Ffoo%2F"), "next"),
            Some("/app/foo/".to_string())
        );
    }

    #[test]
    fn query_param_skips_a_malformed_segment_with_no_equals_sign() {
        assert_eq!(
            query_param(Some("garbage&next=/x/"), "next"),
            Some("/x/".to_string())
        );
    }

    // -- setup_error_message ---------------------------------------------

    #[test]
    fn setup_error_message_maps_every_validation_variant() {
        assert_eq!(
            setup_error_message(&SetupError::EmptyUsername),
            "Username is required."
        );
        assert_eq!(
            setup_error_message(&SetupError::EmptyPassword),
            "Password is required."
        );
        assert_eq!(
            setup_error_message(&SetupError::PasswordMismatch),
            "Passwords do not match."
        );
        assert_eq!(
            setup_error_message(&SetupError::WeakPassword("too short".to_string())),
            "too short"
        );
    }
}
