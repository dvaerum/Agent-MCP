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

pub(crate) fn header_str<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
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
/// principal resolution (see this module's own doc). `pub(crate)`:
/// `oidc_handlers.rs` (Phase E2 PR22 step 7) is a second real
/// consumer -- both the login/setup and OIDC-flow routes sit outside
/// the session gate for the identical reason.
pub(crate) struct RequestMount {
    canonical_path: String,
    is_trusted: bool,
    forwarded_prefix: Option<String>,
    forwarded_proto: Option<String>,
    forwarded_host: Option<String>,
    host: String,
}

impl RequestMount {
    pub(crate) fn resolve(
        state: &RouterState,
        addr: SocketAddr,
        raw_path: &str,
        headers: &HeaderMap,
    ) -> Self {
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

    pub(crate) fn external_path(&self, suffix: &str) -> String {
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
    pub(crate) fn external_origin(&self) -> String {
        mount::external_origin(
            "http",
            &self.host,
            self.is_trusted,
            self.forwarded_proto.as_deref(),
            self.forwarded_host.as_deref(),
        )
    }

    /// The trusted-proxy-gated `X-Forwarded-Proto` value --
    /// `login::cookie_secure_flag`'s own second parameter shape
    /// (`None` when the header is absent OR the peer isn't trusted,
    /// matching every existing call site's own `mount_ctx.is_trusted
    /// .then_some(...).flatten()` composition, promoted here once a
    /// second module needed the identical expression).
    pub(crate) fn forwarded_proto_if_trusted(&self) -> Option<&str> {
        self.is_trusted
            .then_some(self.forwarded_proto.as_deref())
            .flatten()
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

pub(crate) fn see_other(location: &str) -> Response {
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
    identity::users_table_is_empty(&state.sea_orm_db)
        .await
        .map_err(|_| ())
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
    let outcome = match login::attempt_login(&state.sea_orm_db, &username, &password).await {
        Ok(o) => o,
        Err(_) => return internal_error(),
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
                match identity::create_session(&conn, &user.user_id, &now_str, &expires) {
                    Ok(s) => s,
                    Err(_) => return internal_error(),
                }
            };
            // `touch_last_login` (users, sea-orm) runs AFTER the
            // `conn` mutex guard above is dropped -- `create_session`
            // (sessions, deliberately still rusqlite) and this write
            // to a different table/row have no ordering dependency on
            // each other, so splitting them across the lock boundary
            // is safe (Phase G router step 4 PR D; sessions itself
            // stays out of scope, see `identity.rs`'s own module doc).
            if identity::touch_last_login(&state.sea_orm_db, &user.user_id, &now_str)
                .await
                .is_err()
            {
                return internal_error();
            }
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
pub(crate) fn require_secure_cookies_env() -> bool {
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

    let outcome = login::attempt_setup(
        &state.sea_orm_db,
        true,
        &username,
        &password,
        &password_confirm,
        email,
        &registered_projects,
        &now_str,
    )
    .await;

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
                match identity::create_session(&conn, &user_id, &now_str, &expires) {
                    Ok(s) => s,
                    Err(_) => return internal_error(),
                }
            };
            // See `login_post_handler`'s own comment on this same
            // split: `touch_last_login` (users, sea-orm) runs after
            // the rusqlite `conn` guard for `create_session`
            // (sessions, deliberately still rusqlite) is dropped.
            if identity::touch_last_login(&state.sea_orm_db, &user_id, &now_str)
                .await
                .is_err()
            {
                return internal_error();
            }
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

/// End-to-end tests over a real `axum::Router` (via `ServiceExt::
/// oneshot`, no real socket needed) -- proves the FULL
/// `login_post_handler`/`setup_post_handler` stack (real `Form`
/// extraction, `enforce_same_origin`, credential checking, status +
/// cookie), not just the pure decision-function halves `login.rs`'s
/// own unit tests already cover. Three test_sec_* pentest-regression
/// ports land here:
///
/// - SEC-R10-F2 (`test_sec_r10_form_field_typeconf.py`): a form field
///   submitted as a multipart FILE part. Python's `request.post()`
///   returns a `FileField` for it, which reaches `.strip()`/argon2
///   uncaught -> 500. Rust's `axum::extract::Form<T>` only ever
///   accepts `Content-Type: application/x-www-form-urlencoded`
///   (checked BEFORE the body is even read) -- a `multipart/form-data`
///   submission is rejected as `FormRejection::InvalidFormContentType`
///   at the extractor layer, landing in the SAME `Ok(Form(body)) =
///   form else { ... }` branch both handlers already use for every
///   other malformed-body case. The vulnerability class (a non-`str`
///   value reaching `.strip()`/argon2) is architecturally impossible
///   here: `LoginFormBody`/`SetupFormBody`'s fields are typed `String`,
///   so there is no `FileField`-shaped value `serde` could ever hand
///   the handler even if multipart parsing were attempted.
/// - AC-R17-1 (`test_sec_r17_sso_login_enum_oracle.py`): already
///   exhaustively covered at the pure-`attempt_login` level by
///   `login.rs`'s own `attempt_login_rejects_an_sso_only_user_with_
///   no_password_hash`/`attempt_login_runs_the_identical_argon2_work_
///   on_every_rejection_path` (and structurally guaranteed beyond
///   that: `password_hash: Option<String>` makes a `None`-hash panic
///   impossible by construction -- there is no `.as_deref()` call that
///   could hand argon2 a null pointer the way Python's
///   `verify_password(None, pw)` did). This is a top-up closing the
///   ONE gap those unit tests don't reach: the real HTTP layer, proving
///   `login_post_handler` itself returns 401 (never 500) for a
///   passwordless SSO-provisioned user.
/// - PF-R21-1 (`test_sec_r21_unicode_decode_body.py`), form tier only
///   (sites 3/4 -- the JSON tier, sites 1/2, already routes through
///   `json_sanitize::decode_untrusted_body`'s own exhaustively-tested
///   `std::str::from_utf8` guard, confirmed by reading every JSON-body-
///   accepting handler in this crate: `lifecycle_rest.rs`/
///   `users_groups_rest.rs` all fuse through `perm_gates::
///   read_body_and_revalidate` -> `decode_untrusted_body`, so no
///   second guard is needed there). For the FORM tier: `axum::extract
///   ::Form`'s underlying `serde_urlencoded`/`form_urlencoded` decode
///   is LOSSY per the WHATWG spec (invalid UTF-8 bytes become
///   U+FFFD), never an `Err` -- a materially SAFER failure mode than
///   Python's strict-decode-or-raise, and never a stack-overflow risk
///   either (form bodies are flat key=value pairs, not recursively
///   nested like JSON). These tests pin the observable contract (never
///   500) end to end regardless of which of the two safe mechanisms
///   (lossy decode -> empty/garbled field -> ordinary invalid-
///   credentials 401, or a genuine `FormRejection`) actually fires.
#[cfg(test)]
mod http_tests {
    use std::net::SocketAddr;
    use std::sync::Arc;

    use axum::body::Body;
    use axum::http::Request;
    use axum::routing::get;
    use axum::Router;
    use tower::ServiceExt;

    use super::*;
    use crate::identity;
    use crate::login::SESSION_COOKIE_NAME;
    use crate::orchestrator::ensure::EnsureConfig;
    use crate::project_registry::ProjectRegistry;
    use crate::rate_limit::RateLimitConfig;
    use crate::state::RouterStateConfig;

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    fn test_state_config() -> RouterStateConfig {
        RouterStateConfig {
            sock_dir: std::path::PathBuf::from("/tmp/agent-mcp-sockets"),
            dashboard_dir: None,
            external_url: None,
            idle_sec: 14400,
            asset_prefix: None,
            single_tenant_name: None,
            single_tenant_workspace: None,
            max_streams_per_agent: 4,
            max_streams_global: 64,
            default_workspace_parent: std::path::PathBuf::from("/tmp/agent-mcp-projects"),
            token_dir: None,
        }
    }

    /// A real login/setup axum sub-router over a freshly built
    /// file-backed `RouterState` -- no session gate / rate-limit /
    /// empty-users-redirect middleware layered on (irrelevant to the
    /// 3 findings above: `/agent-mcp/login`+`/agent-mcp/setup` are
    /// `path_policy::UNAUTH_PREFIXES`-exempt from all three in the
    /// real app anyway). `_dir` must outlive the router (the project
    /// registry's backing file AND the shared sqlite file both live
    /// under it). File-backed (not `:memory:`), same dual-connection
    /// recipe as `identity.rs`'s own `conn_with_sea_orm` -- these
    /// tests seed users through `identity::create_user`/
    /// `create_sso_user` (sea-orm, Phase G router step 4 PR D) and the
    /// real handlers under test read the SAME rows back through
    /// `state.sea_orm_db`; two separate `:memory:` handles (or a
    /// sqlx pool opening more than one physical connection to one
    /// `:memory:` URI) would each see their own empty database.
    async fn test_app() -> (tempfile::TempDir, Arc<RouterState>, Router) {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("login_setup_test.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conexus_db::schema::init_router_schema(&conn).unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let sea_orm_db = sea_orm::Database::connect(format!("sqlite://{}", db_path.display()))
            .await
            .unwrap();
        let state = Arc::new(RouterState::new(
            conn,
            sea_orm_db,
            registry,
            RateLimitConfig::resolve_from_process_env(),
            EnsureConfig::from_env(|_| None),
            test_state_config(),
        ));
        let router = Router::new()
            .route(
                "/agent-mcp/login",
                get(login_get_handler).post(login_post_handler),
            )
            .route(
                "/agent-mcp/setup",
                get(setup_get_handler).post(setup_post_handler),
            )
            .with_state(Arc::clone(&state));
        (dir, state, router)
    }

    fn peer_addr() -> SocketAddr {
        "127.0.0.1:9999".parse().unwrap()
    }

    async fn post(
        router: &Router,
        path: &str,
        content_type: &str,
        body: Vec<u8>,
    ) -> (StatusCode, HeaderMap) {
        let mut req = Request::builder()
            .method("POST")
            .uri(path)
            .header(header::CONTENT_TYPE, content_type)
            .body(Body::from(body))
            .unwrap();
        // `ConnectInfo` is normally populated by
        // `into_make_service_with_connect_info` off a real accepted
        // connection; `oneshot` drives the `Router` directly with no
        // listener, so it's supplied by hand the same way a real
        // socket's peer address would be.
        req.extensions_mut()
            .insert(axum::extract::ConnectInfo(peer_addr()));
        let resp = router.clone().oneshot(req).await.unwrap();
        (resp.status(), resp.headers().clone())
    }

    /// Same as `post` above, but also collects the response body --
    /// needed by AC-R17-1's byte-identical-body assertion, which `post`
    /// alone (status/headers only) can't prove.
    async fn post_body(
        router: &Router,
        path: &str,
        content_type: &str,
        body: Vec<u8>,
    ) -> (StatusCode, String) {
        let mut req = Request::builder()
            .method("POST")
            .uri(path)
            .header(header::CONTENT_TYPE, content_type)
            .body(Body::from(body))
            .unwrap();
        req.extensions_mut()
            .insert(axum::extract::ConnectInfo(peer_addr()));
        let resp = router.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = http_body_util::BodyExt::collect(resp.into_body())
            .await
            .unwrap()
            .to_bytes();
        (status, String::from_utf8_lossy(&bytes).into_owned())
    }

    /// A real multipart/form-data body with one field forced into a
    /// FILE part (`filename=` present) -- the exact aiohttp
    /// `FormData.add_field(..., filename=...)` shape
    /// `test_sec_r10_form_field_typeconf.py` sends.
    fn multipart_body(boundary: &str, username_is_file: bool, password_is_file: bool) -> Vec<u8> {
        fn part(boundary: &str, name: &str, value: &str, as_file: bool) -> String {
            if as_file {
                format!(
                    "--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; \
                     filename=\"evil.txt\"\r\nContent-Type: application/octet-stream\r\n\r\n\
                     {value}\r\n"
                )
            } else {
                format!(
                    "--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n\
                     {value}\r\n"
                )
            }
        }
        let mut body = String::new();
        body.push_str(&part(boundary, "username", "alice", username_is_file));
        body.push_str(&part(boundary, "password", "hunter2", password_is_file));
        body.push_str(&format!("--{boundary}--\r\n"));
        body.into_bytes()
    }

    // -- SEC-R10-F2: multipart file-part fields must never 500 --------

    #[tokio::test]
    async fn login_post_rejects_a_multipart_body_with_401_not_500() {
        let (_dir, _state, router) = test_app().await;
        let body = multipart_body("XBOUNDARY", true, true);
        let (status, _headers) = post(
            &router,
            "/agent-mcp/login",
            "multipart/form-data; boundary=XBOUNDARY",
            body,
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn login_post_password_only_as_a_file_part_is_401_not_500() {
        let (_dir, state, router) = test_app().await;
        identity::create_user(
            &state.sea_orm_db,
            "bob",
            "hunter2pw123",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let body = multipart_body("XBOUNDARY", false, true);
        let (status, _headers) = post(
            &router,
            "/agent-mcp/login",
            "multipart/form-data; boundary=XBOUNDARY",
            body,
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn setup_post_rejects_a_multipart_body_with_400_not_500() {
        let (_dir, _state, router) = test_app().await;
        let body = multipart_body("XBOUNDARY", true, false);
        let (status, _headers) = post(
            &router,
            "/agent-mcp/setup",
            "multipart/form-data; boundary=XBOUNDARY",
            body,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    // -- Regression: genuine urlencoded text fields keep working --------

    #[tokio::test]
    async fn login_post_urlencoded_credentials_still_authenticate() {
        let (_dir, state, router) = test_app().await;
        identity::create_user(
            &state.sea_orm_db,
            "carol",
            "hunter2pw123",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        let (status, headers) = post(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            b"username=carol&password=hunter2pw123".to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::SEE_OTHER);
        let set_cookie = headers
            .get(header::SET_COOKIE)
            .expect("correct login must set a cookie")
            .to_str()
            .unwrap();
        assert!(set_cookie.contains(SESSION_COOKIE_NAME));
    }

    #[tokio::test]
    async fn setup_post_urlencoded_fields_still_create_the_first_operator() {
        let (_dir, state, router) = test_app().await;
        let (status, headers) = post(
            &router,
            "/agent-mcp/setup",
            "application/x-www-form-urlencoded",
            b"username=first_op&password=secret-pw-1234&password_confirm=secret-pw-1234".to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::SEE_OTHER);
        assert!(headers.get(header::SET_COOKIE).is_some());
        assert!(
            identity::get_user_by_username(&state.sea_orm_db, "first_op")
                .await
                .unwrap()
                .is_some()
        );
    }

    // -- AC-R17-1 top-up: SSO/passwordless user must not 500 over HTTP --

    #[tokio::test]
    async fn login_post_sso_passwordless_user_is_401_not_500() {
        let (_dir, state, router) = test_app().await;
        identity::create_sso_user(
            &state.sea_orm_db,
            "sso-victim",
            "sub-1",
            None,
            false,
            false,
            NOW,
        )
        .await
        .unwrap();
        let (status, headers) = post(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            b"username=sso-victim&password=anything".to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert!(
            headers.get(header::SET_COOKIE).is_none(),
            "an SSO/passwordless account must never mint a session"
        );
    }

    #[tokio::test]
    async fn login_post_sso_account_body_is_byte_identical_to_a_deleted_account() {
        // Port of `test_sec_r17_sso_login_enum_oracle.py::test_sso_
        // login_response_byte_identical_to_unknown_user`. The login
        // form echoes the submitted username, so bodies for DIFFERENT
        // usernames legitimately differ -- the real oracle is whether
        // *the same* submitted username reveals, via status or body,
        // that it names an SSO account. POST one fixed username while
        // it's an SSO row, delete the row, then POST the identical
        // request again: both responses must be byte-identical 401s.
        let (_dir, state, router) = test_app().await;
        // A second account keeps the users table non-empty after the
        // probe row is deleted -- login.rs's own empty-users gate is
        // out of scope for this router-level test harness (see
        // `test_app`'s own doc), but keeping the shape faithful to the
        // real deployment topology costs nothing.
        identity::create_user(
            &state.sea_orm_db,
            "keep-nonempty",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        identity::create_sso_user(
            &state.sea_orm_db,
            "probe-user",
            "sub-probe",
            None,
            false,
            false,
            NOW,
        )
        .await
        .unwrap();

        let body = b"username=probe-user&password=guess".to_vec();
        let (status_sso, body_sso) = post_body(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            body.clone(),
        )
        .await;

        {
            let conn = state.conn.lock().await;
            conn.execute("DELETE FROM users WHERE username = 'probe-user'", [])
                .unwrap();
        }
        let (status_missing, body_missing) = post_body(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            body,
        )
        .await;

        assert_eq!(status_sso, StatusCode::UNAUTHORIZED);
        assert_eq!(status_missing, StatusCode::UNAUTHORIZED);
        assert_eq!(
            body_sso, body_missing,
            "an SSO account must be byte-identical to a nonexistent username"
        );
    }

    #[tokio::test]
    async fn login_post_sso_account_status_and_copy_match_wrong_password_path() {
        // Port of `test_sec_r17_sso_login_enum_oracle.py::test_sso_
        // login_status_matches_wrong_password_path`.
        let (_dir, state, router) = test_app().await;
        identity::create_user(
            &state.sea_orm_db,
            "pw-real",
            "rightpw12345",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .await
        .unwrap();
        identity::create_sso_user(
            &state.sea_orm_db,
            "sso-user",
            "sub-sso",
            None,
            false,
            false,
            NOW,
        )
        .await
        .unwrap();

        let (status_sso, body_sso) = post_body(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            b"username=sso-user&password=guess".to_vec(),
        )
        .await;
        let (status_badpw, body_badpw) = post_body(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            b"username=pw-real&password=guess".to_vec(),
        )
        .await;

        assert_eq!(status_sso, StatusCode::UNAUTHORIZED);
        assert_eq!(status_badpw, StatusCode::UNAUTHORIZED);
        assert!(body_sso.contains("Invalid username or password."));
        assert!(body_badpw.contains("Invalid username or password."));
    }

    // -- PF-R21-1 (form tier): invalid-UTF8 body must never 500 ---------

    #[tokio::test]
    async fn login_post_invalid_utf8_body_is_401_not_500() {
        let (_dir, _state, router) = test_app().await;
        let (status, headers) = post(
            &router,
            "/agent-mcp/login",
            "application/x-www-form-urlencoded",
            b"{\"k\":\"\xff\xfe\xfd\"}".to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert!(headers.get(header::SET_COOKIE).is_none());
    }

    #[tokio::test]
    async fn setup_post_invalid_utf8_body_is_4xx_not_500() {
        let (_dir, _state, router) = test_app().await;
        let (status, _headers) = post(
            &router,
            "/agent-mcp/setup",
            "application/x-www-form-urlencoded",
            b"{\"k\":\"\xff\xfe\xfd\"}".to_vec(),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
}
