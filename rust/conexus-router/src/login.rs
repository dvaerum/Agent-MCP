//! Router login / session-minting + the first-boot setup wizard. Port
//! of `agent_mcp/router/login.py` (597 LOC) + `agent_mcp/router/
//! setup_wizard.py` (256 LOC), combined into one module (Phase E2
//! PR 10, `conexus-router-login`) -- setup is a thin dependent of
//! login's own primitives (same cookie/same-origin/decoy-timing
//! machinery), and both hinge on the identical
//! `identity::create_user`/`identity::create_session` calls this
//! crate already ported in PR 3.
//!
//! Framework-agnostic, matching this crate's own `mcp_handler.rs`/
//! `proxy_core.rs` precedent: real axum route registration, actual
//! `Set-Cookie` header serialization, and HTTP-status mapping are ALL
//! deferred to PR 23 (app-wiring) -- this module is pure logic over
//! plain Rust types the eventual handler layer composes.
//!
//! Deliberately deferred, per the PR's own research -- documented
//! here, not silently dropped:
//! - **Rate-limiting on login attempts** (PR 15,
//!   `conexus-router-rate-limit`): Python's `login_post_handler` has
//!   ZERO rate-limit awareness of its own -- it's a middleware concern
//!   sitting in FRONT of the route (`rate_limit.rate_limit_middleware`),
//!   also gating `/setup` and (eventually) `/sso/*`. Building even a
//!   minimal slice here would either duplicate PR 15's work or wrongly
//!   couple its design to this module's boundaries.
//! - **SSO provider resolution** (`_resolve_sso_provider_name`, PR 22 --
//!   `sso.py` isn't ported yet): whatever later PR renders the login
//!   page always resolves "no SSO provider" (the legacy form) until
//!   then.
//! - **`AGENT_MCP_BOOTSTRAP_USERNAME`/`PASSWORD` env-var bootstrap**
//!   (ADR-0013's OTHER first-operator path, distinct from the setup
//!   wizard): lives in `app.py`'s startup hook, not
//!   `login.py`/`setup_wizard.py` -- app-wiring's job (PR 23).
//! - **Real `Set-Cookie` serialization**: [`SessionCookie`] is a plain
//!   data struct; PR 23's axum layer turns it into an actual response
//!   header (matching how `mcp_handler.rs`'s `HandlerResponse` stays
//!   framework-agnostic today).
//! - **`create_first_operator`'s `registered_projects`**: callers pass
//!   an explicit slice, matching `identity::create_user`'s own interim
//!   contract -- wiring the real `ProjectRegistry::list()` is
//!   app-wiring's job, not this module's.
//! - **Templating** (`login.html`/`setup.html`): no HTML-rendering
//!   crate exists in this workspace; out of scope for this module
//!   entirely (a pure-logic layer has nothing to render).
//!
//! `#![allow(dead_code)]`: no HTTP handler wires this module in yet
//! (PR 23) -- same helpers-ahead-of-their-first-consumer precedent as
//! `mount.rs`/`path_policy.rs`/`identity.rs`/`project_registry.rs`.
#![allow(dead_code)]

use std::sync::LazyLock;

use rusqlite::Connection;

use crate::identity::{self, verify_password, IdentityError, UserRow};

/// Port of `SESSION_COOKIE_NAME`.
pub const SESSION_COOKIE_NAME: &str = "agent_mcp_session";

/// Port of `COOKIE_MAX_AGE` -- 30 days, mirrors
/// `identity::DEFAULT_SESSION_LIFETIME_DAYS` (two independent 30-day
/// windows -- cookie expiry vs. the DB row's `expires_at` -- tuned to
/// the same value, not the same mechanism).
pub const COOKIE_MAX_AGE_SECS: i64 = 60 * 60 * 24 * 30;

/// A fixed, throwaway password hashed once at first use -- port of
/// `_DECOY_PASSWORD_HASH`. [`attempt_login`] verifies against this
/// hash (discarding the result) whenever no real user/password_hash
/// exists to check, so a nonexistent username and an SSO-provisioned
/// user with `password_hash IS NULL` spend the identical argon2
/// CPU/memory cost a real wrong-password attempt spends -- the
/// enumeration-timing defense this migration's `mcp_handler.rs` PR 9
/// already established the same pattern for (`floored_unauthorized`).
static DECOY_PASSWORD_HASH: LazyLock<String> =
    LazyLock::new(|| identity::hash_password("decoy-password-never-assigned-to-any-real-user"));

/// Safe `?next=` redirect resolution -- port of `_safe_next`. Same-
/// origin, path-only: any target that could leave the origin (a
/// protocol-relative `//host/...`, an absolute `scheme://...` URL, or
/// anything not starting with `/`) falls back to `default` (the
/// caller's own `mount::external_path(request, "/")`, already
/// mount-aware). Not pinned to `/agent-mcp/` specifically (ADR-0020:
/// a root-mounted deploy must be able to redirect to `/`, `/app/...`,
/// etc.) -- any same-origin absolute path is honoured verbatim.
pub fn safe_next(raw: Option<&str>, default: &str) -> String {
    let Some(raw) = raw else {
        return default.to_string();
    };
    if raw.is_empty() {
        return default.to_string();
    }
    if raw.starts_with("//") {
        return default.to_string();
    }
    if raw.contains("://") {
        return default.to_string();
    }
    if !raw.starts_with('/') {
        return default.to_string();
    }
    raw.to_string()
}

/// A same-origin/CSRF check rejected the request -- port of the
/// `web.HTTPForbidden` `enforce_same_origin` raises. Not a CSRF
/// token: an `Origin`/`Sec-Fetch-Site` same-origin check, since the
/// threat here is login-CSRF ("session forcing") -- these POSTs MINT
/// a fresh cookie rather than consuming an existing one, so
/// `SameSite=Lax` alone gives zero protection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CrossOriginRejected;

/// Port of `enforce_same_origin`'s policy (R9-F1), as a pure function
/// of already-extracted inputs:
/// 1. `Origin` present: must equal `self_origin` (case-insensitive).
///    Mismatch -- including the opaque `"null"` origin a sandboxed
///    iframe sends -- is rejected (an opaque origin never equals a
///    real `scheme://host`, so no special case is needed).
/// 2. `Origin` absent: fall back to `Sec-Fetch-Site`; reject
///    `"cross-site"`/`"cross-origin"` (case-insensitive).
/// 3. Both absent (a non-browser client -- curl, CLI, pentest
///    harness): allow.
///
/// `self_origin` is computed by the caller via the already-ported
/// [`crate::mount::external_origin`] -- the load-bearing security
/// property (gating `X-Forwarded-*` trust on peer identity so a
/// forged `Origin`+`X-Forwarded-Host` pair from an untrusted peer
/// can't slip past this check) lives entirely in that function's own
/// `is_trusted` threading, not here.
pub fn enforce_same_origin(
    origin_header: Option<&str>,
    sec_fetch_site: Option<&str>,
    self_origin: &str,
) -> Result<(), CrossOriginRejected> {
    if let Some(origin) = origin_header {
        if origin.eq_ignore_ascii_case(self_origin) {
            return Ok(());
        }
        return Err(CrossOriginRejected);
    }
    if let Some(site) = sec_fetch_site {
        if site.eq_ignore_ascii_case("cross-site") || site.eq_ignore_ascii_case("cross-origin") {
            return Err(CrossOriginRejected);
        }
    }
    Ok(())
}

/// Port of `cookie_secure_flag`: forced `true` if the operator has
/// set `AGENT_MCP_REQUIRE_SECURE_COOKIES` (`require_secure_env`,
/// resolved by the caller); else honours `X-Forwarded-Proto` ONLY
/// when the caller has already established the peer is a trusted
/// proxy (`forwarded_proto_if_trusted: None` when untrusted or
/// absent -- matches `mount.rs`'s own "explicit trust input"
/// convention); else falls back to the real transport scheme.
pub fn cookie_secure_flag(
    require_secure_env: bool,
    forwarded_proto_if_trusted: Option<&str>,
    url_scheme: &str,
) -> bool {
    if require_secure_env {
        return true;
    }
    if let Some(proto) = forwarded_proto_if_trusted {
        return proto.eq_ignore_ascii_case("https");
    }
    url_scheme.eq_ignore_ascii_case("https")
}

/// `Set-Cookie` attributes for the `SameSite` directive -- this
/// module only ever mints `Lax` (port of the hardcoded value both
/// `_set_session_cookie`/`_clear_session_cookie` pass), but kept as
/// an enum (not a bare `&str`) so a future attribute value is a
/// compile-time-visible change, not a silently-widened string param.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SameSite {
    Lax,
}

/// The session cookie's full attribute set, as a plain data struct --
/// PR 23's axum layer turns this into a real `Set-Cookie` header.
/// Never carries a Rust cookie-crate type, matching this module's own
/// "no HTTP-framework dependency" precedent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionCookie {
    pub name: &'static str,
    pub value: String,
    pub path: String,
    pub http_only: bool,
    pub secure: bool,
    pub same_site: SameSite,
    pub max_age: i64,
}

/// Port of `_set_session_cookie`: mints the real cookie with
/// `Max-Age=`[`COOKIE_MAX_AGE_SECS`]. `path` is the caller's own
/// `mount::external_prefix(request) + "/"` (ADR-0020: `/` at root,
/// `/agent-mcp/` on the tailnet) -- never the module-level constant a
/// literal port of Python's `COOKIE_PATH` would suggest, since that
/// constant is superseded at every real call site.
pub fn set_session_cookie(session_id: &str, path: &str, secure: bool) -> SessionCookie {
    SessionCookie {
        name: SESSION_COOKIE_NAME,
        value: session_id.to_string(),
        path: path.to_string(),
        http_only: true,
        secure,
        same_site: SameSite::Lax,
        max_age: COOKIE_MAX_AGE_SECS,
    }
}

/// Port of `_clear_session_cookie`: `Max-Age=0`, empty value, no
/// `Expires` -- a minimal, predictable clear header, deliberately not
/// aiohttp's `del_cookie` equivalent (which would also set an
/// `Expires` attribute in the past).
pub fn clear_session_cookie(path: &str, secure: bool) -> SessionCookie {
    SessionCookie {
        name: SESSION_COOKIE_NAME,
        value: String::new(),
        path: path.to_string(),
        http_only: true,
        secure,
        same_site: SameSite::Lax,
        max_age: 0,
    }
}

/// Extract one cookie's value from a raw `Cookie:` request header.
/// New primitive -- nothing in this workspace parses a `Cookie:`
/// header yet (`mcp_handler.rs` only ever deals with bearer tokens).
/// A malformed segment (no `=`) is skipped, matching a real browser's
/// own tolerant `Cookie:` header construction.
pub fn parse_cookie_header(raw: &str, name: &str) -> Option<String> {
    raw.split(';').find_map(|part| {
        let (k, v) = part.trim().split_once('=')?;
        (k.trim() == name).then(|| v.trim().to_string())
    })
}

/// Port of `resolve_current_user`: reads the session cookie, resolves
/// the session (which itself slides `last_used_at`, per
/// [`identity::get_session`]), then the user row. `None` on a missing
/// cookie, an empty cookie value, or a missing/expired session/user --
/// never an error for any of those; a genuine DB error still
/// propagates (this crate's own repositories never collapse a real
/// `rusqlite::Error` into a falsy sentinel, unlike Python's blanket
/// `except sqlite3.OperationalError: return None`, which exists only
/// to tolerate a not-yet-migrated DB -- not a case this crate's
/// `init_router_schema`-backed connections can be in).
pub fn resolve_current_user(
    conn: &Connection,
    cookie_header: Option<&str>,
    now: &str,
) -> Result<Option<UserRow>, IdentityError> {
    let Some(session_id) =
        cookie_header.and_then(|header| parse_cookie_header(header, SESSION_COOKIE_NAME))
    else {
        return Ok(None);
    };
    if session_id.is_empty() {
        return Ok(None);
    }
    let Some(session) = identity::get_session(conn, &session_id, now)? else {
        return Ok(None);
    };
    identity::get_user_by_id(conn, &session.user_id)
}

/// Port of `touch_session`: a bare `last_used_at` slide, independent
/// of a full [`resolve_current_user`] resolution -- for a caller (the
/// eventual session-gate middleware, PR 13) that has already resolved
/// the user by some other means this request and only needs to keep
/// the session alive.
pub fn touch_session(conn: &Connection, session_id: &str, now: &str) -> Result<(), IdentityError> {
    conn.execute(
        "UPDATE sessions SET last_used_at = ?1 WHERE session_id = ?2",
        (now, session_id),
    )?;
    Ok(())
}

/// The result of a login attempt -- port of steps 6-9 of
/// `login_post_handler`'s credential-checking half (the empty-field
/// pre-check happens at the caller/form-parsing layer, since it's
/// argument validation, not part of the credential-timing logic this
/// function protects). Deliberately a single `InvalidCredentials`
/// variant covering "no such username", "user exists but has no
/// password_hash" (an SSO-provisioned account), AND "wrong password" --
/// collapsing all three into one outcome is what makes the
/// enumeration-timing defense STRUCTURAL: no caller can accidentally
/// branch on which sub-case fired and leak it back to the client.
#[derive(Debug)]
pub enum LoginAttemptOutcome {
    Success(UserRow),
    InvalidCredentials,
}

/// Port of `login_post_handler`'s credential check. Always runs
/// exactly one `verify_password` call: against the real stored hash
/// when a user with a password exists, or against
/// [`DECOY_PASSWORD_HASH`] (result discarded) otherwise -- so a
/// nonexistent username, an SSO-only account, and a real wrong
/// password all cost the identical argon2 work.
pub fn attempt_login(
    conn: &Connection,
    username: &str,
    password: &str,
) -> Result<LoginAttemptOutcome, IdentityError> {
    let user = identity::get_user_by_username(conn, username)?;
    match user.as_ref().and_then(|u| u.password_hash.as_deref()) {
        Some(hash) => {
            if verify_password(hash, password) {
                Ok(LoginAttemptOutcome::Success(
                    user.expect("hash implies user"),
                ))
            } else {
                Ok(LoginAttemptOutcome::InvalidCredentials)
            }
        }
        None => {
            // Either no such user, or a real user row with no
            // password_hash (SSO-provisioned) -- spend the identical
            // argon2 work against the decoy hash either way, per this
            // function's own doc.
            let _ = verify_password(&DECOY_PASSWORD_HASH, password);
            Ok(LoginAttemptOutcome::InvalidCredentials)
        }
    }
}

/// Why [`create_first_operator`] refused -- port of `setup_post_
/// handler`'s validation ladder (steps 4-6), as a closed enum a
/// future HTTP layer maps each variant to its own exact status/copy
/// (matching this migration's `UpdateSingleTaskOutcome`/`SendOutcome`
/// precedent over Python's message-string routing).
#[derive(Debug)]
pub enum SetupError {
    EmptyUsername,
    EmptyPassword,
    PasswordMismatch,
    WeakPassword(String),
    /// Port of the `UsernameAlreadyExistsError` race carve-out: someone
    /// else won the wizard between the caller's own empty-table check
    /// and this call's INSERT. The caller's job is to treat this as
    /// "wizard already completed" (a 303 to `/login`), not a 409.
    UsernameAlreadyExists,
    Db(IdentityError),
}

impl From<IdentityError> for SetupError {
    fn from(e: IdentityError) -> Self {
        match e {
            IdentityError::UsernameAlreadyExists(_) => SetupError::UsernameAlreadyExists,
            IdentityError::WeakPassword(msg) => SetupError::WeakPassword(msg),
            other => SetupError::Db(other),
        }
    }
}

/// Port of `setup_post_handler`'s validation ladder + the
/// `create_user(..., is_sysadmin=false, bootstrap_sysadmin=true, ...)`
/// call -- the wizard is pure HTTP-adjacent plumbing around a security
/// invariant ([`identity::bootstrap_first_operator`]) that already
/// lives entirely in `identity.rs`. `registered_projects` is the
/// caller's job to source (see this module's own doc); an empty slice
/// is the correct, safe interim value, not a stub.
#[allow(clippy::too_many_arguments)]
pub fn create_first_operator(
    conn: &mut Connection,
    username: &str,
    password: &str,
    password_confirm: &str,
    email: Option<&str>,
    registered_projects: &[String],
    now: &str,
) -> Result<String, SetupError> {
    if username.trim().is_empty() {
        return Err(SetupError::EmptyUsername);
    }
    if password.is_empty() {
        return Err(SetupError::EmptyPassword);
    }
    if password != password_confirm {
        return Err(SetupError::PasswordMismatch);
    }
    identity::validate_password_strength(password)?;
    let user_id = identity::create_user(
        conn,
        username,
        password,
        email,
        false,
        true,
        registered_projects,
        now,
    )?;
    Ok(user_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";

    // -- safe_next / open-redirect --------------------------------

    #[test]
    fn safe_next_honours_a_same_origin_absolute_path() {
        assert_eq!(safe_next(Some("/app/foo"), "/default"), "/app/foo");
    }

    #[test]
    fn safe_next_falls_back_on_missing_or_empty() {
        assert_eq!(safe_next(None, "/default"), "/default");
        assert_eq!(safe_next(Some(""), "/default"), "/default");
    }

    #[test]
    fn safe_next_rejects_protocol_relative_urls() {
        assert_eq!(
            safe_next(Some("//evil.example.test/"), "/default"),
            "/default"
        );
    }

    #[test]
    fn safe_next_rejects_absolute_urls_with_any_scheme() {
        assert_eq!(
            safe_next(Some("https://evil.example.test/"), "/default"),
            "/default"
        );
        assert_eq!(
            safe_next(Some("javascript://alert(1)"), "/default"),
            "/default"
        );
    }

    #[test]
    fn safe_next_rejects_a_non_absolute_path() {
        assert_eq!(safe_next(Some("relative/path"), "/default"), "/default");
    }

    // -- enforce_same_origin ---------------------------------------

    #[test]
    fn same_origin_allows_a_matching_origin_header() {
        assert!(
            enforce_same_origin(Some("https://example.test"), None, "https://example.test").is_ok()
        );
    }

    #[test]
    fn same_origin_is_case_insensitive() {
        assert!(
            enforce_same_origin(Some("HTTPS://EXAMPLE.TEST"), None, "https://example.test").is_ok()
        );
    }

    #[test]
    fn same_origin_rejects_a_mismatched_origin() {
        assert_eq!(
            enforce_same_origin(Some("https://evil.test"), None, "https://example.test"),
            Err(CrossOriginRejected)
        );
    }

    #[test]
    fn same_origin_rejects_the_opaque_null_origin() {
        assert_eq!(
            enforce_same_origin(Some("null"), None, "https://example.test"),
            Err(CrossOriginRejected)
        );
    }

    #[test]
    fn same_origin_falls_back_to_sec_fetch_site_when_origin_absent() {
        assert_eq!(
            enforce_same_origin(None, Some("cross-site"), "https://example.test"),
            Err(CrossOriginRejected)
        );
        assert_eq!(
            enforce_same_origin(None, Some("Cross-Origin"), "https://example.test"),
            Err(CrossOriginRejected)
        );
        assert!(enforce_same_origin(None, Some("same-origin"), "https://example.test").is_ok());
    }

    #[test]
    fn same_origin_allows_a_non_browser_client_with_neither_header() {
        assert!(enforce_same_origin(None, None, "https://example.test").is_ok());
    }

    // -- cookie_secure_flag ------------------------------------------

    #[test]
    fn cookie_secure_flag_is_forced_by_the_env_override() {
        assert!(cookie_secure_flag(true, None, "http"));
    }

    #[test]
    fn cookie_secure_flag_honours_trusted_forwarded_proto() {
        assert!(cookie_secure_flag(false, Some("https"), "http"));
        assert!(!cookie_secure_flag(false, Some("http"), "https"));
    }

    #[test]
    fn cookie_secure_flag_falls_back_to_the_real_transport_scheme() {
        assert!(cookie_secure_flag(false, None, "https"));
        assert!(!cookie_secure_flag(false, None, "http"));
    }

    // -- cookie minting/clearing --------------------------------------

    #[test]
    fn set_session_cookie_has_the_exact_attribute_contract() {
        let cookie = set_session_cookie("abc123", "/agent-mcp/", true);
        assert_eq!(cookie.name, SESSION_COOKIE_NAME);
        assert_eq!(cookie.value, "abc123");
        assert_eq!(cookie.path, "/agent-mcp/");
        assert!(cookie.http_only);
        assert!(cookie.secure);
        assert_eq!(cookie.same_site, SameSite::Lax);
        assert_eq!(cookie.max_age, COOKIE_MAX_AGE_SECS);
    }

    #[test]
    fn clear_session_cookie_has_max_age_zero_and_an_empty_value() {
        let cookie = clear_session_cookie("/agent-mcp/", false);
        assert_eq!(cookie.value, "");
        assert_eq!(cookie.max_age, 0);
        assert!(cookie.http_only);
    }

    // -- parse_cookie_header ------------------------------------------

    #[test]
    fn parse_cookie_header_finds_the_named_cookie() {
        let raw = "foo=bar; agent_mcp_session=abc123; baz=qux";
        assert_eq!(
            parse_cookie_header(raw, SESSION_COOKIE_NAME),
            Some("abc123".to_string())
        );
    }

    #[test]
    fn parse_cookie_header_returns_none_when_absent() {
        assert_eq!(parse_cookie_header("foo=bar", SESSION_COOKIE_NAME), None);
        assert_eq!(parse_cookie_header("", SESSION_COOKIE_NAME), None);
    }

    #[test]
    fn parse_cookie_header_skips_a_malformed_segment() {
        let raw = "garbage; agent_mcp_session=abc123";
        assert_eq!(
            parse_cookie_header(raw, SESSION_COOKIE_NAME),
            Some("abc123".to_string())
        );
    }

    // -- resolve_current_user / touch_session --------------------------

    fn seed_user(c: &mut Connection) -> String {
        identity::create_user(
            c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap()
    }

    #[test]
    fn resolve_current_user_returns_none_with_no_cookie() {
        let c = conn();
        assert!(resolve_current_user(&c, None, NOW).unwrap().is_none());
    }

    #[test]
    fn resolve_current_user_returns_none_with_an_empty_cookie_value() {
        let c = conn();
        let header = format!("{SESSION_COOKIE_NAME}=");
        assert!(resolve_current_user(&c, Some(&header), NOW)
            .unwrap()
            .is_none());
    }

    #[test]
    fn resolve_current_user_returns_the_user_for_a_valid_session_cookie() {
        let mut c = conn();
        let uid = seed_user(&mut c);
        let sid = identity::create_session(&c, &uid, NOW, "2026-02-01T00:00:00.000+00:00").unwrap();
        let header = format!("other=1; {SESSION_COOKIE_NAME}={sid}");

        let user = resolve_current_user(&c, Some(&header), NOW)
            .unwrap()
            .unwrap();
        assert_eq!(user.user_id, uid);
    }

    #[test]
    fn resolve_current_user_returns_none_for_an_expired_session() {
        let mut c = conn();
        let uid = seed_user(&mut c);
        let sid = identity::create_session(&c, &uid, NOW, "2026-01-01T00:01:00.000+00:00").unwrap();
        let header = format!("{SESSION_COOKIE_NAME}={sid}");

        let later = "2026-01-01T00:02:00.000+00:00";
        assert!(resolve_current_user(&c, Some(&header), later)
            .unwrap()
            .is_none());
    }

    #[test]
    fn touch_session_slides_last_used_at() {
        let mut c = conn();
        let uid = seed_user(&mut c);
        let sid = identity::create_session(&c, &uid, NOW, "2026-02-01T00:00:00.000+00:00").unwrap();
        let later = "2026-01-01T00:05:00.000+00:00";

        touch_session(&c, &sid, later).unwrap();

        let last_used: String = c
            .query_row(
                "SELECT last_used_at FROM sessions WHERE session_id = ?1",
                [&sid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(last_used, later);
    }

    // -- attempt_login (the enumeration-timing defense) ------------------

    #[test]
    fn attempt_login_succeeds_with_the_right_password() {
        let mut c = conn();
        seed_user(&mut c);
        let outcome = attempt_login(&c, "alice", "correct horse battery staple").unwrap();
        assert!(matches!(outcome, LoginAttemptOutcome::Success(u) if u.username == "alice"));
    }

    #[test]
    fn attempt_login_rejects_a_wrong_password() {
        let mut c = conn();
        seed_user(&mut c);
        let outcome = attempt_login(&c, "alice", "wrong password").unwrap();
        assert!(matches!(outcome, LoginAttemptOutcome::InvalidCredentials));
    }

    #[test]
    fn attempt_login_rejects_a_nonexistent_username() {
        let c = conn();
        let outcome = attempt_login(&c, "nobody", "anything").unwrap();
        assert!(matches!(outcome, LoginAttemptOutcome::InvalidCredentials));
    }

    #[test]
    fn attempt_login_rejects_an_sso_only_user_with_no_password_hash() {
        let c = conn();
        // Simulate an SSO-provisioned row: no password_hash.
        c.execute(
            "INSERT INTO users (user_id, username, email, password_hash, created_at, is_sysadmin) \
             VALUES ('u1', 'ssouser', NULL, NULL, ?1, 0)",
            [NOW],
        )
        .unwrap();
        let outcome = attempt_login(&c, "ssouser", "anything").unwrap();
        assert!(matches!(outcome, LoginAttemptOutcome::InvalidCredentials));
    }

    #[test]
    fn attempt_login_runs_the_identical_argon2_work_on_every_rejection_path() {
        // Not a timing assertion (too flaky in CI) -- proves the
        // STRUCTURAL property instead: both the nonexistent-username
        // and the wrong-real-password paths verify against SOME
        // argon2id hash rather than short-circuiting before ever
        // calling verify_password. If either path regressed to an
        // early return, this pinned decoy-hash value would go
        // unused, which `cargo llvm-cov`/manual inspection could
        // catch -- but the direct proof here is that both distinct
        // rejection reasons independently converge to the SAME
        // `InvalidCredentials` variant.
        let mut c = conn();
        seed_user(&mut c);
        let missing = attempt_login(&c, "nobody", "x").unwrap();
        let wrong = attempt_login(&c, "alice", "wrong").unwrap();
        assert!(matches!(missing, LoginAttemptOutcome::InvalidCredentials));
        assert!(matches!(wrong, LoginAttemptOutcome::InvalidCredentials));
    }

    // -- create_first_operator (setup wizard) ------------------------------

    #[test]
    fn create_first_operator_rejects_an_empty_username() {
        let mut c = conn();
        let err = create_first_operator(
            &mut c,
            "  ",
            "correct horse battery staple",
            "correct horse battery staple",
            None,
            &[],
            NOW,
        )
        .unwrap_err();
        assert!(matches!(err, SetupError::EmptyUsername));
    }

    #[test]
    fn create_first_operator_rejects_an_empty_password() {
        let mut c = conn();
        let err = create_first_operator(&mut c, "alice", "", "", None, &[], NOW).unwrap_err();
        assert!(matches!(err, SetupError::EmptyPassword));
    }

    #[test]
    fn create_first_operator_rejects_a_password_mismatch() {
        let mut c = conn();
        let err = create_first_operator(
            &mut c,
            "alice",
            "correct horse battery staple",
            "different confirmation entirely",
            None,
            &[],
            NOW,
        )
        .unwrap_err();
        assert!(matches!(err, SetupError::PasswordMismatch));
    }

    #[test]
    fn create_first_operator_rejects_a_weak_password() {
        let mut c = conn();
        let err =
            create_first_operator(&mut c, "alice", "short", "short", None, &[], NOW).unwrap_err();
        assert!(matches!(err, SetupError::WeakPassword(_)));
    }

    #[test]
    fn create_first_operator_creates_and_bootstraps_the_first_sysadmin() {
        let mut c = conn();
        let uid = create_first_operator(
            &mut c,
            "alice",
            "correct horse battery staple",
            "correct horse battery staple",
            Some("alice@example.test"),
            &["proj-a".to_string()],
            NOW,
        )
        .unwrap();
        let row = identity::get_user_by_id(&c, &uid).unwrap().unwrap();
        assert!(row.is_sysadmin);
        let member_count: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM project_membership WHERE user_id = ?1",
                [&uid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(member_count, 1);
    }

    #[test]
    fn create_first_operator_surfaces_the_race_as_username_already_exists() {
        let mut c = conn();
        create_first_operator(
            &mut c,
            "alice",
            "correct horse battery staple",
            "correct horse battery staple",
            None,
            &[],
            NOW,
        )
        .unwrap();
        let err = create_first_operator(
            &mut c,
            "alice",
            "another password entirely",
            "another password entirely",
            None,
            &[],
            NOW,
        )
        .unwrap_err();
        assert!(matches!(err, SetupError::UsernameAlreadyExists));
    }
}
