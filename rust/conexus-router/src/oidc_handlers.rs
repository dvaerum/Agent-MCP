//! The two real OIDC authorization-code-flow routes: `GET /agent-mcp/
//! sso/login` (redirect to the IdP) and `GET /agent-mcp/sso/callback`
//! (mint the session). Port target: `agent_mcp/router/sso.py`'s
//! `init_oidc_login_handler`/`handle_oidc_callback`/
//! `_default_redirect_url`/`_resolve_redirect_url` (Phase E2 PR22
//! step 7/8, `conexus-router-oidc-handlers`) -- the composition point
//! for every prior PR22 step (1 `SsoSubject`, 3 `oidc_reconcile`, 4
//! `oidc_group_mapping`, 5 `oidc_http_client`, 6 `oidc_flow_state`).
//!
//! **Operator decision 2026-09-06 (typed-vs-raw ID-token claims,
//! Option B)**: `sub`/`email`/`preferred_username`/`email_verified`
//! all come from `openidconnect`'s spec-typed `CoreIdTokenClaims`
//! accessors, never a raw JSON re-parse -- a malformed claim there
//! already hard-failed `IdToken::claims()` before this handler ever
//! runs (see `oidc_http_client.rs`'s own module doc for the full
//! rationale). A real, practical consequence, confirmed here: under
//! Option B, `claims.subject()`/`.email()`/`.preferred_username()`
//! are ALWAYS well-typed strings by the time this handler sees them,
//! so several of Python's own defensive `isinstance(..., str)`
//! degrade-to-None guards (R16-F1) have NO Rust equivalent to port --
//! the type system already guarantees what those guards checked for
//! at runtime.
//!
//! **`groups` is the one deliberate exception**: it's a non-standard
//! claim `CoreIdTokenClaims` (fixed to `EmptyAdditionalClaims`) never
//! models at all -- not a gap in Option B's typed-claims choice, a
//! genuinely different concern (a claim shape the crate's `Core*`
//! convenience aliases don't cover, full stop). Re-deriving it via a
//! custom `AdditionalClaims` generic would mean hand-spelling all 11
//! of `Client`'s other type parameters throughout this module for one
//! field. Instead, [`extract_groups_claim`] reads it directly off the
//! ALREADY-SIGNATURE-VERIFIED raw JWT payload (by the time it runs,
//! `IdToken::claims()` already succeeded) -- a second read of
//! already-trusted bytes, not a raw-reparse SECURITY fallback the way
//! Python's R16-F1 guards were. Malformed `groups` degrades to empty,
//! matching Python's own tolerant `if not isinstance(groups_claim,
//! list): groups_claim = []` -- `apply_group_mapping`'s own
//! established "a convenience feature, not a security gate" posture,
//! not Option B's stricter one (`groups` was never in Option B's
//! scope to begin with).

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{ConnectInfo, OriginalUri, RawQuery, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::Utc;
use openidconnect::core::CoreResponseType;
use openidconnect::{
    AuthenticationFlow, AuthorizationCode, CsrfToken, IssuerUrl, Nonce, PkceCodeChallenge,
    PkceCodeVerifier, Scope,
};

use crate::identity;
use crate::login_setup_rest::{header_str, RequestMount};
use crate::oidc_flow_state::{
    clear_flow_cookie, decode_flow_cookie, encode_flow_cookie, set_flow_cookie, FlowState,
};
use crate::oidc_group_mapping::{apply_group_mapping, reconcile_oidc_group_membership};
use crate::oidc_http_client::{build_oidc_client, fetch_oidc_metadata};
use crate::oidc_reconcile::{find_or_create_oidc_user, OidcReconcileInput};
use crate::sso;
use crate::sso_subject::SsoSubject;
use crate::state::RouterState;

fn plain_text_response(status: StatusCode, body: &'static str) -> Response {
    (status, [(header::CONTENT_TYPE, "text/plain")], body).into_response()
}

fn missing_params() -> Response {
    plain_text_response(StatusCode::BAD_REQUEST, "missing oidc callback params")
}
fn invalid_state() -> Response {
    plain_text_response(StatusCode::BAD_REQUEST, "invalid oidc state")
}
fn invalid_flow() -> Response {
    plain_text_response(StatusCode::BAD_REQUEST, "invalid oidc flow")
}
fn discovery_failed() -> Response {
    plain_text_response(StatusCode::BAD_GATEWAY, "OIDC discovery failed")
}
fn token_exchange_failed() -> Response {
    plain_text_response(StatusCode::BAD_GATEWAY, "OIDC token exchange failed")
}
fn missing_id_token() -> Response {
    plain_text_response(
        StatusCode::BAD_GATEWAY,
        "OIDC token response missing id_token",
    )
}
fn id_token_validation_failed() -> Response {
    plain_text_response(StatusCode::BAD_GATEWAY, "OIDC id_token validation failed")
}

/// Port of `_default_redirect_url`/`_resolve_redirect_url`. Explicit
/// config wins, then `AGENT_MCP_EXTERNAL_URL`, then `derived_origin`
/// -- the mount-aware origin `RequestMount::external_origin()`
/// already resolves (the same trusted-proxy-gated host/scheme
/// composition `login.rs::_external_origin`'s Rust port already
/// establishes). Takes the already-resolved origin as a plain `&str`
/// rather than `&RequestMount` directly -- a pure function with no
/// dependency on that type, fully unit-testable without constructing
/// a `RouterState` (this crate's own "explicit input over hidden
/// dependency" convention, `mount.rs`'s `is_trusted: bool` precedent).
fn resolve_oidc_redirect_url(
    cfg: &sso::OidcSettings,
    external_url_env: Option<&str>,
    derived_origin: &str,
) -> String {
    if let Some(configured) = &cfg.redirect_url {
        return configured.clone();
    }
    if let Some(external) = external_url_env.map(str::trim).filter(|s| !s.is_empty()) {
        return format!("{}/agent-mcp/sso/callback", external.trim_end_matches('/'));
    }
    format!("{derived_origin}/agent-mcp/sso/callback")
}

/// The one non-standard claim `CoreIdTokenClaims` never models -- see
/// this module's own doc for why a second, post-verification read of
/// the raw JWT payload is the right seam for it. `raw_id_token` is
/// the string form of an `IdToken` whose signature `IdToken::claims()`
/// has ALREADY verified by the time this is ever called.
fn extract_groups_claim(raw_id_token: &str) -> Vec<String> {
    let Some(payload_b64) = raw_id_token.split('.').nth(1) else {
        return Vec::new();
    };
    let Ok(payload_bytes) = URL_SAFE_NO_PAD.decode(payload_b64) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(&payload_bytes) else {
        return Vec::new();
    };
    match value.get("groups") {
        Some(serde_json::Value::Array(items)) => items
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect(),
        _ => Vec::new(),
    }
}

/// The outcome of resolving the real, loaded OIDC config -- shared
/// entry gate both handlers below run first. `NotActive` means
/// "SSO isn't in OIDC mode" (404, matching Python's `raise web.
/// HTTPNotFound()`); a config LOAD failure is `ConfigError`, mapped
/// to a 500 by callers exactly like Python's own unguarded
/// `get_sso_config()` call (no try/except around it in either real
/// handler).
enum OidcConfigOutcome {
    Active(sso::OidcSettings),
    NotActive,
    /// A config LOAD failure -- callers map this to a 500, matching
    /// Python's own unguarded `get_sso_config()` call (no try/except
    /// around it in either real handler). Carries no message
    /// (clippy::result_large_err: a full `Response` as an `Err`
    /// variant is >128 bytes -- same fix shape as the earlier
    /// `login_setup_rest.rs::users_table_is_empty` catch).
    ConfigError,
}

fn resolve_active_oidc_config() -> OidcConfigOutcome {
    let settings = match sso::load_sso_config(
        |key| std::env::var(key).ok(),
        |path| std::fs::read_to_string(path),
    ) {
        Ok(s) => s,
        Err(_) => return OidcConfigOutcome::ConfigError,
    };
    if settings.mode != sso::SsoMode::Oidc {
        return OidcConfigOutcome::NotActive;
    }
    match settings.oidc {
        Some(cfg) => OidcConfigOutcome::Active(cfg),
        None => OidcConfigOutcome::NotActive,
    }
}

// ── GET /agent-mcp/sso/login ─────────────────────────────────────────

pub async fn init_oidc_login_handler(
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    State(state): State<Arc<RouterState>>,
) -> Response {
    let cfg = match resolve_active_oidc_config() {
        OidcConfigOutcome::Active(cfg) => cfg,
        OidcConfigOutcome::NotActive => return StatusCode::NOT_FOUND.into_response(),
        OidcConfigOutcome::ConfigError => {
            return plain_text_response(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
        }
    };

    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);
    let issuer = match IssuerUrl::new(cfg.issuer.clone()) {
        Ok(i) => i,
        Err(_) => return discovery_failed(),
    };

    let http_client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap_or_default();
    let metadata = match fetch_oidc_metadata(&issuer, &http_client).await {
        Ok(m) => m,
        Err(_) => return discovery_failed(),
    };

    let redirect_uri = resolve_oidc_redirect_url(
        &cfg,
        std::env::var("AGENT_MCP_EXTERNAL_URL").ok().as_deref(),
        &mount_ctx.external_origin(),
    );
    let client =
        match build_oidc_client(metadata, &cfg.client_id, &cfg.client_secret, &redirect_uri) {
            Ok(c) => c,
            Err(_) => return discovery_failed(),
        };

    let (pkce_challenge, pkce_verifier) = PkceCodeChallenge::new_random_sha256();
    let mut request = client
        .authorize_url(
            AuthenticationFlow::<CoreResponseType>::AuthorizationCode,
            CsrfToken::new_random,
            Nonce::new_random,
        )
        .set_pkce_challenge(pkce_challenge);
    for scope in &cfg.scopes {
        request = request.add_scope(Scope::new(scope.clone()));
    }
    let (auth_url, csrf_state, nonce) = request.url();

    let cookie_value = encode_flow_cookie(&FlowState {
        state: csrf_state.secret().clone(),
        code_verifier: PkceCodeVerifier::secret(&pkce_verifier).clone(),
        nonce: nonce.secret().clone(),
    });
    let secure = crate::login::cookie_secure_flag(
        crate::login_setup_rest::require_secure_cookies_env(),
        mount_ctx.forwarded_proto_if_trusted(),
        "http",
    );
    let cookie = set_flow_cookie(&cookie_value, secure);

    (
        StatusCode::SEE_OTHER,
        [
            (header::LOCATION, auth_url.to_string()),
            (header::SET_COOKIE, cookie.to_header_value()),
        ],
    )
        .into_response()
}

// ── GET /agent-mcp/sso/callback ──────────────────────────────────────

pub async fn handle_oidc_callback(
    OriginalUri(uri): OriginalUri,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    RawQuery(raw_query): RawQuery,
    State(state): State<Arc<RouterState>>,
) -> Response {
    let cfg = match resolve_active_oidc_config() {
        OidcConfigOutcome::Active(cfg) => cfg,
        OidcConfigOutcome::NotActive => return StatusCode::NOT_FOUND.into_response(),
        OidcConfigOutcome::ConfigError => {
            return plain_text_response(StatusCode::INTERNAL_SERVER_ERROR, "internal error")
        }
    };

    let query = raw_query.unwrap_or_default();
    let state_param = query_param(&query, "state").unwrap_or_default();
    let code_param = query_param(&query, "code").unwrap_or_default();
    let flow_cookie = header_str(&headers, "cookie")
        .and_then(|raw| {
            crate::login::parse_cookie_header(raw, crate::oidc_flow_state::FLOW_COOKIE_NAME)
        })
        .unwrap_or_default();

    if state_param.is_empty() || code_param.is_empty() || flow_cookie.is_empty() {
        return missing_params();
    }
    let Some(flow) = decode_flow_cookie(&flow_cookie) else {
        return invalid_state();
    };
    if flow.state != state_param {
        return invalid_state();
    }
    // Defence in depth (round-3 finding AC-1): decode_flow_cookie
    // already refuses a nonce-less cookie, so this is a redundant
    // guard kept explicit at the trust boundary, matching Python's
    // own identical redundant check.
    if flow.nonce.is_empty() {
        return invalid_flow();
    }

    let mount_ctx = RequestMount::resolve(&state, addr, uri.path(), &headers);
    let issuer = match IssuerUrl::new(cfg.issuer.clone()) {
        Ok(i) => i,
        Err(_) => return discovery_failed(),
    };
    let http_client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap_or_default();
    let metadata = match fetch_oidc_metadata(&issuer, &http_client).await {
        Ok(m) => m,
        Err(_) => return discovery_failed(),
    };

    let redirect_uri = resolve_oidc_redirect_url(
        &cfg,
        std::env::var("AGENT_MCP_EXTERNAL_URL").ok().as_deref(),
        &mount_ctx.external_origin(),
    );
    let client =
        match build_oidc_client(metadata, &cfg.client_id, &cfg.client_secret, &redirect_uri) {
            Ok(c) => c,
            Err(_) => return discovery_failed(),
        };

    let token_response = client
        .exchange_code(AuthorizationCode::new(code_param))
        .set_pkce_verifier(PkceCodeVerifier::new(flow.code_verifier.clone()))
        .request_async(&http_client)
        .await;
    let token_response = match token_response {
        Ok(resp) => resp,
        Err(_) => return token_exchange_failed(),
    };

    let Some(id_token) = token_response.extra_fields().id_token() else {
        return missing_id_token();
    };
    let raw_id_token = id_token.to_string();
    let nonce = Nonce::new(flow.nonce);
    let claims = match id_token.claims(&client.id_token_verifier(), &nonce) {
        Ok(c) => c,
        Err(_) => return id_token_validation_failed(),
    };

    // Option B (see this module's own doc): every accessor below is
    // already well-typed by construction -- `claims` only exists
    // because signature+iss+aud+exp+nonce verification already
    // succeeded.
    let email = claims.email().map(|e| e.as_str());
    let email_verified = claims.email_verified() == Some(true);
    let preferred_username_claim = claims.preferred_username().map(|u| u.as_str());
    let subject_str = claims.subject().as_str();
    let preferred_username = preferred_username_claim.or(Some(subject_str));

    let oidc_subject = SsoSubject::from_claims(
        Some(claims.issuer().as_str()),
        Some(&serde_json::Value::String(subject_str.to_string())),
    );
    let legacy_subject = oidc_subject
        .as_ref()
        .and_then(SsoSubject::legacy_lookup_key);
    let Some(oidc_subject) = oidc_subject else {
        return id_token_validation_failed();
    };

    let groups_claim = extract_groups_claim(&raw_id_token);

    let now = Utc::now();
    let now_str = now.to_rfc3339();
    let mut conn = state.conn.lock().await;
    let user = match find_or_create_oidc_user(
        &mut conn,
        &OidcReconcileInput {
            email,
            email_verified,
            preferred_username,
            subject: &oidc_subject,
            default_is_sysadmin: cfg.default_is_sysadmin,
            bootstrap_sysadmin: cfg.default_is_sysadmin,
        },
        &now_str,
    ) {
        Ok(u) => u,
        // R15-F2 (Python's InvalidEmailError: an unpaired UTF-16
        // surrogate crashing the INSERT) has NO Rust equivalent to
        // port -- a `&str`/`String` can never contain an unpaired
        // surrogate (Rust strings are always valid UTF-8 by
        // construction), so this bug class is structurally
        // impossible here, not merely handled.
        Err(_) => return plain_text_response(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    };
    let _ = legacy_subject; // reconciliation already applied inside find_or_create_oidc_user

    if !cfg.group_mapping.is_empty() {
        apply_group_mapping(
            &conn,
            &user.user_id,
            &groups_claim,
            &cfg.group_mapping,
            &now_str,
        );
        // De-provision (round-9 AC-R9-1): revoke IdP-managed (oidc:)
        // memberships the current claim no longer justifies. Manual
        // local grants are out of scope and untouched.
        reconcile_oidc_group_membership(&conn, &user.user_id, &groups_claim, &cfg.group_mapping);
    }

    let expires =
        (now + chrono::Duration::days(identity::DEFAULT_SESSION_LIFETIME_DAYS)).to_rfc3339();
    let session_id = match identity::create_session(&conn, &user.user_id, &now_str, &expires) {
        Ok(s) => s,
        Err(_) => return plain_text_response(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    };
    if identity::touch_last_login(&conn, &user.user_id, &now_str).is_err() {
        return plain_text_response(StatusCode::INTERNAL_SERVER_ERROR, "internal error");
    }
    drop(conn);

    let secure = crate::login::cookie_secure_flag(
        crate::login_setup_rest::require_secure_cookies_env(),
        mount_ctx.forwarded_proto_if_trusted(),
        "http",
    );
    let session_cookie =
        crate::login::set_session_cookie(&session_id, &mount_ctx.external_path(""), secure);
    let flow_clear_cookie = clear_flow_cookie(secure);

    let target = mount_ctx.external_path("/");
    // Two REAL, DISTINCT `Set-Cookie` headers are required here (mint
    // the session, clear the consumed flow cookie) -- axum's tuple-
    // array `IntoResponse` shorthand INSERTS each pair into the
    // `HeaderMap` (last-wins for a repeated name), so a second
    // `(header::SET_COOKIE, ...)` entry silently overwrites the
    // first rather than appending a second header. Caught live (not
    // assumed): the real compiled binary sent only the flow-cookie
    // clear, never the session cookie, before this fix. `HeaderMap::
    // append` is the correct primitive for "more than one value of
    // the same header name."
    let mut headers = HeaderMap::new();
    headers.insert(
        header::LOCATION,
        target.parse().expect("valid header value"),
    );
    headers.append(
        header::SET_COOKIE,
        session_cookie
            .to_header_value()
            .parse()
            .expect("valid header value"),
    );
    headers.append(
        header::SET_COOKIE,
        flow_clear_cookie
            .to_header_value()
            .parse()
            .expect("valid header value"),
    );
    (StatusCode::SEE_OTHER, headers).into_response()
}

/// Hand-rolled, lenient query-param extraction -- same rationale as
/// `login_setup_rest.rs::query_param`: this is an unauthenticated
/// route, so a malformed query string degrades to "param absent"
/// rather than 400ing via `axum::extract::Query`.
fn query_param(raw_query: &str, name: &str) -> Option<String> {
    raw_query.split('&').find_map(|pair| {
        let (k, v) = pair.split_once('=')?;
        (k == name).then(|| {
            percent_encoding::percent_decode_str(v)
                .decode_utf8_lossy()
                .into_owned()
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oidc_settings(redirect_url: Option<&str>) -> sso::OidcSettings {
        sso::OidcSettings {
            issuer: "https://idp.example.test".to_string(),
            client_id: "client-id".to_string(),
            client_secret: "secret".to_string(),
            provider_name: "SSO".to_string(),
            group_mapping: std::collections::HashMap::new(),
            redirect_url: redirect_url.map(str::to_string),
            scopes: vec!["openid".to_string()],
            default_is_sysadmin: false,
        }
    }

    #[test]
    fn resolve_oidc_redirect_url_prefers_the_explicit_config_value() {
        let cfg = oidc_settings(Some("https://configured.example.test/callback"));
        assert_eq!(
            resolve_oidc_redirect_url(
                &cfg,
                Some("https://env.example.test"),
                "https://derived.example.test"
            ),
            "https://configured.example.test/callback"
        );
    }

    #[test]
    fn resolve_oidc_redirect_url_falls_back_to_the_external_url_env_var() {
        let cfg = oidc_settings(None);
        assert_eq!(
            resolve_oidc_redirect_url(
                &cfg,
                Some("https://env.example.test/"),
                "https://derived.example.test"
            ),
            "https://env.example.test/agent-mcp/sso/callback"
        );
    }

    #[test]
    fn resolve_oidc_redirect_url_ignores_a_blank_external_url_env_var() {
        let cfg = oidc_settings(None);
        assert_eq!(
            resolve_oidc_redirect_url(&cfg, Some("   "), "https://derived.example.test"),
            "https://derived.example.test/agent-mcp/sso/callback"
        );
    }

    #[test]
    fn resolve_oidc_redirect_url_falls_back_to_the_derived_origin() {
        let cfg = oidc_settings(None);
        assert_eq!(
            resolve_oidc_redirect_url(&cfg, None, "https://derived.example.test"),
            "https://derived.example.test/agent-mcp/sso/callback"
        );
    }

    #[test]
    fn extract_groups_claim_reads_a_real_array_claim() {
        let payload = serde_json::json!({"groups": ["admins", "engineers"]}).to_string();
        let token = format!(
            "eyJhbGciOiJSUzI1NiJ9.{}.sig",
            URL_SAFE_NO_PAD.encode(payload.as_bytes())
        );
        assert_eq!(
            extract_groups_claim(&token),
            vec!["admins".to_string(), "engineers".to_string()]
        );
    }

    #[test]
    fn extract_groups_claim_degrades_a_missing_claim_to_empty() {
        let payload = serde_json::json!({"sub": "alice"}).to_string();
        let token = format!(
            "eyJhbGciOiJSUzI1NiJ9.{}.sig",
            URL_SAFE_NO_PAD.encode(payload.as_bytes())
        );
        assert!(extract_groups_claim(&token).is_empty());
    }

    #[test]
    fn extract_groups_claim_degrades_a_non_array_claim_to_empty() {
        // Python's own tolerant posture (`if not isinstance(groups_claim,
        // list): groups_claim = []`) -- a misconfigured IdP sending
        // `"groups": "admins"` (a bare string, not an array) must not
        // panic or propagate a type error.
        let payload = serde_json::json!({"groups": "admins"}).to_string();
        let token = format!(
            "eyJhbGciOiJSUzI1NiJ9.{}.sig",
            URL_SAFE_NO_PAD.encode(payload.as_bytes())
        );
        assert!(extract_groups_claim(&token).is_empty());
    }

    #[test]
    fn extract_groups_claim_drops_non_string_array_elements() {
        let payload = serde_json::json!({"groups": ["admins", 42, "engineers"]}).to_string();
        let token = format!(
            "eyJhbGciOiJSUzI1NiJ9.{}.sig",
            URL_SAFE_NO_PAD.encode(payload.as_bytes())
        );
        assert_eq!(
            extract_groups_claim(&token),
            vec!["admins".to_string(), "engineers".to_string()]
        );
    }

    #[test]
    fn extract_groups_claim_degrades_a_malformed_jwt_to_empty() {
        assert!(extract_groups_claim("not-a-jwt-at-all").is_empty());
        assert!(extract_groups_claim("").is_empty());
        assert!(extract_groups_claim("a.b").is_empty());
    }
}
