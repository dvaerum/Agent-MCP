//! Operator-session auth gate for the router's dashboard/REST surface.
//! Port of `agent_mcp/router/auth_middleware.py`'s
//! `require_operator_session_middleware` (Phase E2 PR 12,
//! `conexus-router-session-gate`).
//!
//! Framework-agnostic like every other handler-layer module in this
//! crate (`mcp_handler.rs`/`proxy_core.rs`): [`GateRequest`]/
//! [`SessionGateOutcome`] are plain Rust types over already-extracted
//! inputs; real axum middleware registration is PR 23's job.
//!
//! Deliberately deferred, documented not silently dropped:
//! - **Proxy-header SSO identity** (`_try_proxy_header_identity`,
//!   `sso.py`, PR 22): a caller with no valid session cookie always
//!   falls through to the same "no session" rejection this module
//!   returns. Once PR 22 lands, its own caller resolves proxy-header
//!   identity FIRST and only calls into this gate on a miss --
//!   matching how PR 9's `mcp_handler.rs` deferred the cookie-
//!   authenticated MCP proxy path until session machinery existed.
//! - **A genuinely nonexistent project is NOT rejected here.**
//!   Ported deliberately, not a gap: when [`resolved_project_from_path`]
//!   can't resolve the URL segment to any real/aliased project at
//!   all, this gate admits the request with `project: None` and lets
//!   the REAL downstream handler (the proxy / REST layer) produce its
//!   own "unknown project" response independently. This gate only
//!   intercepts the "project IS real, caller has no membership row"
//!   case -- and [`unknown_project_response`] is built to be
//!   byte-identical to what the downstream handler independently
//!   produces for a truly nonexistent project (SEC round 3, PF-1: a
//!   status/body differential between the two would be a cross-tenant
//!   project-existence oracle).
#![allow(dead_code)]

use std::collections::HashSet;

use chrono::{DateTime, Utc};
use conexus_auth::capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
use conexus_core::capability::ProjectRole;
use conexus_core::principal::{Principal, PrincipalKind};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC};
use rusqlite::Connection;

use crate::identity::{IdentityError, UserRow};
use crate::login;
use crate::mcp_handler::{self, HandlerBody, HandlerResponse};
use crate::orchestrator::resolve as project_resolve;
use crate::path_policy;
use crate::project_registry::ProjectRegistry;
use crate::single_tenant::bypasses_operator_gate;

/// HTTP methods treated as mutations for the per-project operator/
/// viewer split -- port of `_MUTATION_METHODS`. GET/HEAD/OPTIONS (and
/// anything else outside this set) are reads and admit on either
/// tier.
const MUTATION_METHODS: &[&str] = &["POST", "PATCH", "DELETE", "PUT"];

/// Process-wide config this gate needs -- mirrors `McpHandlerConfig`'s
/// own shape (`mcp_handler.rs`). `extra_exact_paths` is the caller-
/// supplied derived public-route set `path_policy::is_unauth_path`
/// itself already takes explicitly (see that module's doc for why).
#[derive(Debug, Clone, Default)]
pub struct SessionGateConfig {
    pub single_tenant_name: Option<String>,
    pub extra_exact_paths: Vec<String>,
}

/// Already-extracted per-request inputs. `path` MUST already be
/// `mount::canonical_path`'d by the caller (this crate's own
/// "canonical path in" convention -- `path_policy.rs`, `login.rs`'s
/// `should_redirect_to_setup`). `login_url` is the caller's own
/// `mount::external_path(request, "/login")` -- this module stays
/// mount-agnostic rather than re-deriving it.
pub struct GateRequest<'a> {
    pub path: &'a str,
    /// `request.path_qs` -- the raw, UNcanonicalized "path?query" the
    /// client sent, preserved verbatim into the login redirect's
    /// `?next=` deep link.
    pub raw_path_qs: &'a str,
    /// Uppercase HTTP method (`"GET"`, `"POST"`, ...).
    pub method: &'a str,
    pub accept_header: Option<&'a str>,
    pub cookie_header: Option<&'a str>,
    pub login_url: &'a str,
}

/// The gate's decision -- port of `require_operator_session_middleware`'s
/// 5 top-to-bottom branches, collapsed into one enum a future axum
/// layer matches exhaustively.
#[derive(Debug)]
pub enum SessionGateOutcome {
    /// Not a `/agent-mcp/*` path, an unauth-allowlisted path, an
    /// ADR-0021 delivery route, or single-tenant bypass -- proceed to
    /// the handler with no principal. `warm_authorized` mirrors
    /// Python's `request["_warm_authorized"]` stash: only the
    /// single-tenant bypass sets it (SC-R6-1), never the bare unauth/
    /// delivery passthroughs.
    PassThrough { warm_authorized: bool },
    /// A non-member's `/agent-mcp/app/<project>/...` request -- proceed
    /// to the handler (the bare SPA shell has no project data behind
    /// it), but do NOT stash a principal or authorize a warm-start.
    PublicAppShell,
    /// Reject outright; the caller renders `HandlerResponse` verbatim.
    Reject(HandlerResponse),
    /// Admit, with this resolved identity. Boxed: `GateIdentity` (a
    /// `UserRow` + `Principal`) is much larger than every other
    /// variant, and this enum is returned by value from every call.
    Allow(Box<GateIdentity>),
}

/// The resolved identity for an [`SessionGateOutcome::Allow`] --
/// port of what Python stashes as `request["user"]`/
/// `request["is_sysadmin"]`/`request["principal"]`.
#[derive(Debug, Clone)]
pub struct GateIdentity {
    pub user: UserRow,
    pub is_sysadmin: bool,
    /// The real, alias-resolved project this request targets. `None`
    /// for a router-admin (non-project-scoped) path, OR a URL segment
    /// that doesn't resolve to any real/aliased project at all (see
    /// this module's own doc for why that case is NOT rejected here).
    pub project: Option<String>,
    pub project_role: Option<ProjectRole>,
    pub principal: Principal,
}

/// `true` iff the caller is a browser asking for HTML -- port of
/// `_wants_html`. Deliberately conservative: no `Accept` header at
/// all is `false` (the safe default for non-browser tooling like a
/// bare `curl`).
pub fn wants_html(accept_header: Option<&str>) -> bool {
    let Some(accept) = accept_header else {
        return false;
    };
    // Presence check is case-SENSITIVE in the real Python source
    // (`"text/html" not in accept`) -- only the first-media-type
    // comparison below is lowercased. Preserved as-is.
    if !accept.contains("text/html") {
        return false;
    }
    let first = accept
        .split(',')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    !first.contains("json")
}

/// Percent-encoding set matching Python's `urllib.parse.quote(...,
/// safe="/")`: everything `NON_ALPHANUMERIC` encodes, MINUS the
/// characters Python's `quote` always treats as safe regardless of
/// `safe=` (`_`, `.`, `-`, `~`) and the explicit `safe="/"` override.
/// `pub(crate)`, not private -- `dashboard_handlers.rs::index_handler`
/// needs the identical `quote()`-safe set to percent-encode
/// `single_tenant_name` into its `/app/<name>/` redirect target,
/// matching Python's own `quote(SINGLE_TENANT_NAME)` call.
pub(crate) const QUOTE_SAFE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'/')
    .remove(b'_')
    .remove(b'.')
    .remove(b'-')
    .remove(b'~');

/// Port of `_login_redirect_response`: a 303 to `login_url` with the
/// original path+query preserved (percent-encoded) as `?next=`, so
/// `login.rs`'s `safe_next` can bounce the operator back after a
/// successful login.
fn login_redirect_response(login_url: &str, raw_path_qs: &str) -> HandlerResponse {
    let encoded = percent_encoding::utf8_percent_encode(raw_path_qs, QUOTE_SAFE).to_string();
    HandlerResponse {
        status: 303,
        headers: vec![(
            "Location".to_string(),
            format!("{login_url}?next={encoded}"),
        )],
        body: HandlerBody::Empty,
    }
}

/// Port of `_unauth_response`: the JSON envelope the dashboard's
/// `ApiClient` keys off (`error: "login_required"`) to redirect to
/// the login page.
fn unauthorized_response(message: &str, login_url: &str) -> HandlerResponse {
    HandlerResponse {
        status: 401,
        headers: vec![],
        body: HandlerBody::Json(serde_json::json!({
            "error": "login_required",
            "message": message,
            "login_url": login_url,
        })),
    }
}

/// Port of the viewer-tier mutation rejection's JSON body.
fn forbidden_response(username: &str, url_segment: &str) -> HandlerResponse {
    HandlerResponse {
        status: 403,
        headers: vec![],
        body: HandlerBody::Json(serde_json::json!({
            "error": "forbidden",
            "message": format!(
                "viewer-tier operator '{username}' cannot mutate project '{url_segment}'"
            ),
        })),
    }
}

/// Port of `app.unknown_project_response`: the SAME wire shape for
/// "this project doesn't exist" and "this project exists but you're
/// not a member" (SEC round 3, PF-1) -- reuses `mcp_handler.rs`'s own
/// Accept-version-gate response so the two independently-built
/// responses can never drift apart.
fn unknown_project_response(method: &str, accept_header: Option<&str>) -> HandlerResponse {
    let accept = accept_header.unwrap_or("");
    if method != "OPTIONS" && !mcp_handler::accept_includes_strict_api_media(accept) {
        return mcp_handler::api_version_required_response();
    }
    HandlerResponse {
        status: 404,
        headers: vec![],
        body: HandlerBody::Text("unknown project".to_string()),
    }
}

pub(crate) fn parse_project_role(raw: &str) -> Option<ProjectRole> {
    match raw {
        "operator" => Some(ProjectRole::Operator),
        "viewer" => Some(ProjectRole::Viewer),
        _ => None,
    }
}

/// Port of `_resolved_project_from_path`: `(url_segment,
/// real_project_name)`. Reuses the already-ported
/// [`project_resolve::resolve`] (PR 9's `orchestrator::resolve`)
/// directly -- it already implements the exact "exact match, else a
/// live alias, else unknown" resolution order this function needs,
/// so there's nothing left to re-derive here.
fn resolved_project_from_path(
    registry: &ProjectRegistry,
    path: &str,
    now: DateTime<Utc>,
) -> (Option<String>, Option<String>) {
    let Some(segment) = path_policy::project_segment_from_path(path) else {
        return (None, None);
    };
    match project_resolve::resolve(registry, segment, now) {
        Ok((real_name, _alias)) => (Some(segment.to_string()), Some(real_name)),
        Err(_) => (Some(segment.to_string()), None),
    }
}

/// The gate itself -- port of `require_operator_session_middleware`.
/// Any genuine DB error resolving the session cookie propagates
/// (this crate's own "distinguish 'not available' from 'available
/// but broken'" convention); sysadmin/group/project-role resolution
/// deliberately fail CLOSED to `None`/`false` on error, matching
/// Python's own defensive `except Exception` around each of those
/// three calls.
#[allow(clippy::too_many_arguments)]
pub fn evaluate_session_gate(
    conn: &Connection,
    registry: &ProjectRegistry,
    cfg: &SessionGateConfig,
    now: DateTime<Utc>,
    req: &GateRequest,
) -> Result<SessionGateOutcome, IdentityError> {
    // Defensive parity with Python -- mount::canonical_path always
    // yields an `/agent-mcp`-prefixed path in practice, so this never
    // actually fires for a caller following this crate's own
    // convention; kept for a 1:1 branch match.
    if !req.path.starts_with("/agent-mcp") {
        return Ok(SessionGateOutcome::PassThrough {
            warm_authorized: false,
        });
    }
    let extra_exact: Vec<&str> = cfg.extra_exact_paths.iter().map(String::as_str).collect();
    if path_policy::is_unauth_path(req.path, &extra_exact) {
        return Ok(SessionGateOutcome::PassThrough {
            warm_authorized: false,
        });
    }
    if path_policy::is_delivery_path(req.path) {
        return Ok(SessionGateOutcome::PassThrough {
            warm_authorized: false,
        });
    }
    if bypasses_operator_gate(cfg.single_tenant_name.as_deref()) {
        return Ok(SessionGateOutcome::PassThrough {
            warm_authorized: true,
        });
    }

    let now_str = now.to_rfc3339();
    let user = login::resolve_current_user(conn, req.cookie_header, &now_str)?;
    let Some(user) = user else {
        // Proxy-header SSO identity (`_try_proxy_header_identity`) is
        // PR 22 territory -- deliberately not attempted here, see
        // this module's own doc.
        let response = if wants_html(req.accept_header) {
            login_redirect_response(req.login_url, req.raw_path_qs)
        } else {
            unauthorized_response("session cookie missing or invalid", req.login_url)
        };
        return Ok(SessionGateOutcome::Reject(response));
    };

    let groups: Option<HashSet<String>> =
        conexus_db::group_membership_repository::resolve_user_groups(conn, &user.user_id).ok();
    let is_sysadmin = conexus_db::group_membership_repository::resolve_user_is_sysadmin(
        conn,
        &user.user_id,
        groups.as_ref(),
    )
    .unwrap_or(false);

    let (url_segment, project) = resolved_project_from_path(registry, req.path, now);

    let mut role: Option<String> = None;
    if let Some(real_project) = &project {
        if !is_sysadmin {
            role = conexus_db::group_membership_repository::resolve_user_project_role(
                conn,
                &user.user_id,
                real_project,
                groups.as_ref(),
            )
            .ok()
            .flatten();
            if role.is_none() {
                if req.path.starts_with("/agent-mcp/app/") {
                    return Ok(SessionGateOutcome::PublicAppShell);
                }
                return Ok(SessionGateOutcome::Reject(unknown_project_response(
                    req.method,
                    req.accept_header,
                )));
            }
            if MUTATION_METHODS.contains(&req.method) && role.as_deref() != Some("operator") {
                return Ok(SessionGateOutcome::Reject(forbidden_response(
                    &user.username,
                    url_segment.as_deref().unwrap_or(""),
                )));
            }
        }
    }

    let principal_project_role = if is_sysadmin || project.is_none() {
        None
    } else {
        role.as_deref().and_then(parse_project_role)
    };

    let capabilities = resolve_capabilities(
        Some(conn),
        ResolveCapabilitiesInput {
            sysadmin: is_sysadmin,
            kind: PrincipalKind::OperatorSession,
            agent_role: None,
            user_id: Some(&user.user_id),
            project_role: principal_project_role,
            groups: groups.as_ref(),
        },
    )?;

    let principal = Principal {
        kind: PrincipalKind::OperatorSession,
        user_id: Some(user.user_id.clone()),
        agent_id: None,
        project_name: project.clone(),
        project_role: principal_project_role,
        agent_role: None,
        can_wake_loop: false,
        source_token: None,
        capabilities,
    };

    Ok(SessionGateOutcome::Allow(Box::new(GateIdentity {
        user,
        is_sysadmin,
        project,
        project_role: principal_project_role,
        principal,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    fn registry_with(dir: &std::path::Path, name: &str, now: DateTime<Utc>) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        registry
            .register(name, "/ws/proj-a", "python", now)
            .unwrap();
        registry
    }

    const NOW: &str = "2026-01-01T00:00:00.000+00:00";
    fn now_dt() -> DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }

    fn seed_operator(c: &mut Connection, username: &str, is_sysadmin_bootstrap: bool) -> String {
        identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            is_sysadmin_bootstrap,
            &[],
            NOW,
        )
        .unwrap()
    }

    fn cookie_for(c: &Connection, user_id: &str) -> String {
        let sid =
            identity::create_session(c, user_id, NOW, "2026-02-01T00:00:00.000+00:00").unwrap();
        format!("{}={}", login::SESSION_COOKIE_NAME, sid)
    }

    fn base_req<'a>(path: &'a str, cookie: Option<&'a str>) -> GateRequest<'a> {
        GateRequest {
            path,
            raw_path_qs: path,
            method: "GET",
            accept_header: None,
            cookie_header: cookie,
            login_url: "/agent-mcp/login",
        }
    }

    #[test]
    fn passes_through_an_unauth_allowlisted_path() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/login", None);
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        assert!(matches!(
            outcome,
            SessionGateOutcome::PassThrough {
                warm_authorized: false
            }
        ));
    }

    #[test]
    fn passes_through_a_delivery_route() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/proj-a/delivery/stream", None);
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        assert!(matches!(
            outcome,
            SessionGateOutcome::PassThrough {
                warm_authorized: false
            }
        ));
    }

    #[test]
    fn single_tenant_mode_bypasses_and_marks_warm_authorized() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig {
            single_tenant_name: Some("proj-a".to_string()),
            ..Default::default()
        };
        let req = base_req("/agent-mcp/api/proj-a/tasks", None);
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        assert!(matches!(
            outcome,
            SessionGateOutcome::PassThrough {
                warm_authorized: true
            }
        ));
    }

    #[test]
    fn rejects_with_a_401_json_envelope_when_no_session_and_no_html_wanted() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/proj-a/tasks", None);
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Reject(resp) = outcome else {
            panic!("expected Reject, got {outcome:?}");
        };
        assert_eq!(resp.status, 401);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected a JSON body");
        };
        assert_eq!(body["error"], "login_required");
    }

    #[test]
    fn rejects_with_a_303_login_redirect_when_a_browser_wants_html() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let mut req = base_req("/agent-mcp/app/proj-a/", None);
        req.accept_header = Some("text/html,application/xhtml+xml");
        req.raw_path_qs = "/agent-mcp/app/proj-a/?page=memories";
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Reject(resp) = outcome else {
            panic!("expected Reject, got {outcome:?}");
        };
        assert_eq!(resp.status, 303);
        let location = resp
            .headers
            .iter()
            .find(|(k, _)| k == "Location")
            .map(|(_, v)| v.as_str())
            .unwrap();
        assert!(location.starts_with("/agent-mcp/login?next="));
        assert!(location.contains("%3F")); // the embedded `?` stays percent-encoded
    }

    #[test]
    fn admits_a_sysadmin_with_full_capabilities_and_no_project_role() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let uid = seed_operator(&mut c, "alice", true);
        let cookie = cookie_for(&c, &uid);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Allow(identity) = outcome else {
            panic!("expected Allow, got {outcome:?}");
        };
        assert!(identity.is_sysadmin);
        assert_eq!(identity.project.as_deref(), Some("proj-a"));
        assert!(identity.project_role.is_none());
        assert!(matches!(
            identity.principal.capabilities,
            conexus_core::capability::Capabilities::Sysadmin
        ));
    }

    #[test]
    fn admits_a_router_admin_path_with_no_project_and_full_sysadmin_caps() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let uid = seed_operator(&mut c, "alice", true);
        let cookie = cookie_for(&c, &uid);
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/router/agents", Some(&cookie));

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Allow(identity) = outcome else {
            panic!("expected Allow, got {outcome:?}");
        };
        assert!(identity.project.is_none());
    }

    #[test]
    fn falls_through_for_a_genuinely_nonexistent_project() {
        // A registered second operator, member of NOTHING, hitting a
        // URL segment that resolves to no real/aliased project at
        // all -- this gate admits it (project: None) and lets the
        // real downstream handler produce its own 404, per this
        // module's own doc.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let uid = seed_operator(&mut c, "alice", true); // first user -> sysadmin, irrelevant here
        let uid2 = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let _ = uid;
        let cookie = cookie_for(&c, &uid2);
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/does-not-exist/tasks", Some(&cookie));

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Allow(identity) = outcome else {
            panic!("expected Allow (fall-through), got {outcome:?}");
        };
        assert!(identity.project.is_none());
    }

    #[test]
    fn non_member_on_app_path_gets_the_public_shell_outcome() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        seed_operator(&mut c, "alice", true); // first user, sysadmin
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let cookie = cookie_for(&c, &bob);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/app/proj-a/", Some(&cookie));

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        assert!(matches!(outcome, SessionGateOutcome::PublicAppShell));
    }

    #[test]
    fn non_member_on_api_path_gets_the_unknown_project_response() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        seed_operator(&mut c, "alice", true);
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let cookie = cookie_for(&c, &bob);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();
        let mut req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));
        req.accept_header = Some(mcp_handler::API_MEDIA_TYPE);

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Reject(resp) = outcome else {
            panic!("expected Reject, got {outcome:?}");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn non_member_on_api_path_without_the_strict_accept_media_gets_a_406_first() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        seed_operator(&mut c, "alice", true);
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        let cookie = cookie_for(&c, &bob);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();
        let req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));

        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Reject(resp) = outcome else {
            panic!("expected Reject, got {outcome:?}");
        };
        assert_eq!(resp.status, 406);
    }

    #[test]
    fn a_viewer_can_read_but_not_mutate_their_project() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        seed_operator(&mut c, "alice", true);
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'viewer')",
            [&bob],
        )
        .unwrap();
        let cookie = cookie_for(&c, &bob);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();

        let read_req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));
        let read_outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &read_req).unwrap();
        let SessionGateOutcome::Allow(identity) = read_outcome else {
            panic!("expected Allow for a read, got {read_outcome:?}");
        };
        assert_eq!(identity.project_role, Some(ProjectRole::Viewer));

        let mut write_req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));
        write_req.method = "POST";
        let write_outcome =
            evaluate_session_gate(&c, &registry, &cfg, now_dt(), &write_req).unwrap();
        let SessionGateOutcome::Reject(resp) = write_outcome else {
            panic!("expected Reject for a viewer mutation, got {write_outcome:?}");
        };
        assert_eq!(resp.status, 403);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected a JSON body");
        };
        assert_eq!(body["error"], "forbidden");
    }

    #[test]
    fn an_operator_member_can_mutate_their_project() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        seed_operator(&mut c, "alice", true);
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'operator')",
            [&bob],
        )
        .unwrap();
        let cookie = cookie_for(&c, &bob);
        let registry = registry_with(dir.path(), "proj-a", now_dt());
        let cfg = SessionGateConfig::default();

        let mut req = base_req("/agent-mcp/api/proj-a/tasks", Some(&cookie));
        req.method = "POST";
        let outcome = evaluate_session_gate(&c, &registry, &cfg, now_dt(), &req).unwrap();
        let SessionGateOutcome::Allow(identity) = outcome else {
            panic!("expected Allow, got {outcome:?}");
        };
        assert_eq!(identity.project_role, Some(ProjectRole::Operator));
    }
}
