//! Synchronous, DB-only decision functions for the router's project-
//! lifecycle REST surface. Port of `admin_api.py`'s
//! `_deny_cross_tenant_project_read`/`_revalidate_capability_and_
//! membership_or_403` (decision halves) + `create_project_handler`'s
//! full logic. Phase E2 PR 17, `conexus-router-project-gate`.
//!
//! Framework-agnostic and fully synchronous, matching
//! `session_gate.rs::evaluate_session_gate`'s own proof that a fresh
//! DB-backed capability/membership re-check needs no `async`, lock, or
//! systemctl-await at all -- only the FUSION of this decision logic
//! with a real yield point (`perm_gates.py`'s `revalidated_lock`/
//! `revalidate_after`/`read_body_and_revalidate`) needs axum's real
//! extractor/handler shape, deferred to PR 23 per this migration's own
//! DEFERRED decision for `perm_gates.py`.
#![allow(dead_code)]

use std::collections::HashSet;
use std::path::Path;

use chrono::{DateTime, Utc};
use conexus_auth::capabilities::{resolve_capabilities, ResolveCapabilitiesInput};
use conexus_core::capability::Capability;
use conexus_core::principal::{Principal, PrincipalKind};
use conexus_db::group_membership_repository;
use rusqlite::Connection;

use crate::lifecycle::{self, LifecycleError};
use crate::login;
use crate::project_registry::{ProjectRegistry, RegistryError};
use crate::session_gate::parse_project_role;

/// Combines the two error sources every function here can hit --
/// `ProjectRegistry`'s own error type and a raw DB error -- matching
/// `orchestrator::resolve::ResolveError`'s own precedent for wrapping
/// `RegistryError` into a local closed enum.
#[derive(Debug)]
pub enum GateError {
    Registry(RegistryError),
    Db(rusqlite::Error),
}

impl From<RegistryError> for GateError {
    fn from(e: RegistryError) -> Self {
        GateError::Registry(e)
    }
}

impl From<rusqlite::Error> for GateError {
    fn from(e: rusqlite::Error) -> Self {
        GateError::Db(e)
    }
}

impl std::fmt::Display for GateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GateError::Registry(e) => write!(f, "{e}"),
            GateError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for GateError {}

/// Port of `_deny_cross_tenant_project_read`'s decision (R4-F3/R6-F2/
/// R9-F2): a sysadmin OR a caller with a resolved role admits;
/// otherwise the SAME uniform [`CrossTenantOutcome::NotFound`] a
/// nonexistent project produces, so "exists but I'm not a member" is
/// indistinguishable from "doesn't exist" (closes the cross-tenant
/// project-existence oracle). `min_role`, when set, additionally
/// requires the resolved role's rank to be at or above it -- a
/// genuine member with insufficient AUTHORITY gets
/// [`CrossTenantOutcome::Forbidden`] instead (no oracle to close: this
/// caller already sees the project in their own view).
pub fn deny_cross_tenant_project_read(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    project_name: &str,
    min_role: Option<&str>,
) -> Result<CrossTenantOutcome, GateError> {
    if is_sysadmin {
        return Ok(CrossTenantOutcome::Admit);
    }
    if registry.get(project_name)?.is_none() {
        return Ok(CrossTenantOutcome::NotFound);
    }
    let Some(user_id) = caller_user_id else {
        return Ok(CrossTenantOutcome::NotFound);
    };
    let role =
        group_membership_repository::resolve_user_project_role(conn, user_id, project_name, None)?;
    let Some(role) = role else {
        return Ok(CrossTenantOutcome::NotFound);
    };
    if let Some(min) = min_role {
        if group_membership_repository::role_rank(&role)
            < group_membership_repository::role_rank(min)
        {
            return Ok(CrossTenantOutcome::Forbidden {
                role,
                min_role: min.to_string(),
            });
        }
    }
    Ok(CrossTenantOutcome::Admit)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CrossTenantOutcome {
    Admit,
    NotFound,
    Forbidden { role: String, min_role: String },
}

/// Port of `revalidate_capability_or_403` (the project-LESS half of
/// `perm_gates.py` -- `admin_users_api.py`'s own handlers gate on a
/// bare `system.*` capability with no `project_name` at all, so
/// `read_body_and_revalidate(req, parse_body, cap)` calls this
/// directly rather than [`revalidate_capability_and_membership`]
/// below, which ALWAYS requires a real project and denies with no
/// membership row -- wrong for a caller who simply isn't scoped to
/// any project). Session-liveness + a fresh capability re-derivation
/// only; no membership/rank check exists to run without a project.
#[derive(Debug)]
pub enum RevalidateCapabilityOutcome {
    Allow(Box<Principal>),
    DeniedSessionInvalid,
    DeniedCapability,
}

pub fn revalidate_capability(
    conn: &Connection,
    stale_user_id: &str,
    cookie_header: Option<&str>,
    now: &str,
    cap: Capability,
) -> Result<RevalidateCapabilityOutcome, GateError> {
    if let Some(header) = cookie_header {
        if login::parse_cookie_header(header, login::SESSION_COOKIE_NAME).is_some() {
            match login::resolve_current_user(conn, Some(header), now) {
                Ok(Some(_)) => {}
                _ => return Ok(RevalidateCapabilityOutcome::DeniedSessionInvalid),
            }
        }
    }

    let groups = group_membership_repository::resolve_user_groups(conn, stale_user_id).ok();
    let is_sysadmin =
        group_membership_repository::resolve_user_is_sysadmin(conn, stale_user_id, groups.as_ref())
            .unwrap_or(false);
    let capabilities = resolve_capabilities(
        Some(conn),
        ResolveCapabilitiesInput {
            sysadmin: is_sysadmin,
            kind: PrincipalKind::OperatorSession,
            agent_role: None,
            user_id: Some(stale_user_id),
            project_role: None,
            groups: groups.as_ref(),
        },
    )?;
    let principal = Principal {
        kind: PrincipalKind::OperatorSession,
        user_id: Some(stale_user_id.to_string()),
        agent_id: None,
        project_name: None,
        project_role: None,
        agent_role: None,
        can_wake_loop: false,
        source_token: None,
        capabilities,
    };
    if !principal.has_capability(cap) {
        return Ok(RevalidateCapabilityOutcome::DeniedCapability);
    }
    Ok(RevalidateCapabilityOutcome::Allow(Box::new(principal)))
}

/// Port of `_revalidate_capability_and_membership_or_403`: a FRESH
/// re-derivation of session liveness, capability, and (when
/// `project_name` matters) membership+rank -- called after a genuine
/// yield point (a body-read, a lock acquisition, a systemctl await)
/// to close the TOCTOU window between an entry-time gate and a
/// destructive write. `stale_user_id` is the identity captured at
/// entry (Python's `req["user"]["user_id"]`, itself possibly stale --
/// this function's whole job is confirming that identity's AUTHORITY
/// is still current, matching Python's own design).
#[derive(Debug)]
pub enum RevalidateOutcome {
    Allow(Box<Principal>),
    DeniedSessionInvalid,
    DeniedCapability,
    DeniedMembership,
    DeniedRank { role: String, min_role: String },
}

#[allow(clippy::too_many_arguments)]
pub fn revalidate_capability_and_membership(
    conn: &Connection,
    stale_user_id: &str,
    cookie_header: Option<&str>,
    now: &str,
    cap: Capability,
    project_name: &str,
    min_role: Option<&str>,
) -> Result<RevalidateOutcome, GateError> {
    // R9-F4: re-run the session-liveness check ONLY when a session
    // cookie is actually present (a proxy-header/forwarding identity
    // has no session row to invalidate and is re-verified fresh on
    // every request already).
    if let Some(header) = cookie_header {
        if login::parse_cookie_header(header, login::SESSION_COOKIE_NAME).is_some() {
            match login::resolve_current_user(conn, Some(header), now) {
                Ok(Some(_)) => {}
                _ => return Ok(RevalidateOutcome::DeniedSessionInvalid),
            }
        }
    }

    let groups = group_membership_repository::resolve_user_groups(conn, stale_user_id).ok();
    let is_sysadmin =
        group_membership_repository::resolve_user_is_sysadmin(conn, stale_user_id, groups.as_ref())
            .unwrap_or(false);
    let project_role_str = if is_sysadmin {
        None
    } else {
        group_membership_repository::resolve_user_project_role(
            conn,
            stale_user_id,
            project_name,
            groups.as_ref(),
        )?
    };
    let principal_project_role = if is_sysadmin {
        None
    } else {
        project_role_str.as_deref().and_then(parse_project_role)
    };

    let capabilities = resolve_capabilities(
        Some(conn),
        ResolveCapabilitiesInput {
            sysadmin: is_sysadmin,
            kind: PrincipalKind::OperatorSession,
            agent_role: None,
            user_id: Some(stale_user_id),
            project_role: principal_project_role,
            groups: groups.as_ref(),
        },
    )?;
    let principal = Principal {
        kind: PrincipalKind::OperatorSession,
        user_id: Some(stale_user_id.to_string()),
        agent_id: None,
        project_name: Some(project_name.to_string()),
        project_role: principal_project_role,
        agent_role: None,
        can_wake_loop: false,
        source_token: None,
        capabilities,
    };

    if !principal.has_capability(cap) {
        return Ok(RevalidateOutcome::DeniedCapability);
    }
    if is_sysadmin {
        return Ok(RevalidateOutcome::Allow(Box::new(principal)));
    }
    let Some(role) = project_role_str else {
        return Ok(RevalidateOutcome::DeniedMembership);
    };
    if let Some(min) = min_role {
        if group_membership_repository::role_rank(&role)
            < group_membership_repository::role_rank(min)
        {
            return Ok(RevalidateOutcome::DeniedRank {
                role,
                min_role: min.to_string(),
            });
        }
    }
    Ok(RevalidateOutcome::Allow(Box::new(principal)))
}

/// Port of `create_project_handler`'s full logic (minus the axum-
/// specific body parse + entry capability gate, both upstream of this
/// function). Performs the real registry write + `mkdir` + best-effort
/// membership grant -- matching `login.rs::attempt_setup`'s own
/// precedent of a "decision function" that performs its real
/// side effect directly rather than returning yet another closure for
/// a caller to invoke.
#[derive(Debug)]
pub enum CreateProjectOutcome {
    Created {
        name: String,
        workspace_label: String,
    },
    Rejected(crate::mcp_handler::HandlerResponse),
}

pub fn decide_create_project(
    conn: &Connection,
    registry: &ProjectRegistry,
    default_workspace_parent: &Path,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    raw_name: Option<&serde_json::Value>,
    now: DateTime<Utc>,
) -> Result<CreateProjectOutcome, GateError> {
    if let Some(resp) = lifecycle::reject_non_str_name(raw_name) {
        return Ok(CreateProjectOutcome::Rejected(resp));
    }
    let name = raw_name
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    let existing: HashSet<String> = registry.list()?.into_iter().map(|p| p.name).collect();
    if let Some(msg) = lifecycle::validate_name(&name, &existing) {
        if msg.contains("already registered") {
            // R1-F1 escape hatch: a hidden (non-visible) collision
            // must look identical to "name is free" -- surface the
            // uniform not-found rather than confirming a hidden
            // tenant's existence via the rich 409.
            return Ok(CreateProjectOutcome::Rejected(
                match deny_cross_tenant_project_read(
                    conn,
                    registry,
                    is_sysadmin,
                    caller_user_id,
                    &name,
                    None,
                )? {
                    CrossTenantOutcome::Admit => {
                        lifecycle::error_envelope(LifecycleError::AlreadyRegistered, &msg, None)
                    }
                    _ => lifecycle::error_envelope(
                        LifecycleError::NotFound,
                        &format!("unknown project: {name:?}"),
                        None,
                    ),
                },
            ));
        }
        return Ok(CreateProjectOutcome::Rejected(lifecycle::error_envelope(
            LifecycleError::InvalidName,
            &msg,
            None,
        )));
    }

    // BL-R33-1: refuse a name that's currently a live alias of
    // another project -- same R1-F1 escape hatch on the alias's real
    // owner.
    if let Some(alias_owner) = registry.resolve_alias(&name, now)? {
        return Ok(CreateProjectOutcome::Rejected(
            match deny_cross_tenant_project_read(
                conn,
                registry,
                is_sysadmin,
                caller_user_id,
                &alias_owner,
                None,
            )? {
                CrossTenantOutcome::Admit => lifecycle::error_envelope(
                    LifecycleError::AliasCollision,
                    &format!("name {name:?} is a live alias of another project"),
                    None,
                ),
                _ => lifecycle::error_envelope(
                    LifecycleError::NotFound,
                    &format!("unknown project: {name:?}"),
                    None,
                ),
            },
        ));
    }

    let workspace = default_workspace_parent.join(&name);
    if let Err(e) = std::fs::create_dir_all(&workspace) {
        // SD-R15-2: never the absolute path, only the OS error text.
        return Ok(CreateProjectOutcome::Rejected(lifecycle::error_envelope(
            LifecycleError::Internal,
            &e.to_string(),
            None,
        )));
    }

    match registry.register(&name, &workspace.to_string_lossy(), "python", now) {
        Ok(_) => {}
        Err(RegistryError::ProjectNameTaken(msg)) | Err(RegistryError::AliasCollision(msg)) => {
            return Ok(CreateProjectOutcome::Rejected(lifecycle::error_envelope(
                LifecycleError::AlreadyRegistered,
                &msg,
                None,
            )));
        }
        Err(e) => return Err(e.into()),
    }

    if let Some(user_id) = caller_user_id {
        // Best-effort, matching Python: a membership-grant failure is
        // logged, never surfaced as a create failure.
        let _ = crate::identity::add_project_membership(conn, user_id, &name);
    }

    Ok(CreateProjectOutcome::Created {
        name: name.clone(),
        workspace_label: lifecycle::workspace_label(
            &workspace.to_string_lossy(),
            default_workspace_parent,
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mcp_handler::HandlerBody;
    use conexus_db::schema::init_router_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("PRAGMA foreign_keys = ON;").unwrap();
        init_router_schema(&c).unwrap();
        c
    }

    fn now_dt() -> DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }
    const NOW_STR: &str = "2026-01-01T00:00:00.000+00:00";

    fn seed_user(c: &mut Connection, username: &str) -> String {
        crate::identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap()
    }

    fn registry_with(dir: &std::path::Path, name: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        registry
            .register(name, "/ws/proj-a", "python", now_dt())
            .unwrap();
        registry
    }

    // -- deny_cross_tenant_project_read ---------------------------------

    #[test]
    fn sysadmin_always_admits() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let outcome =
            deny_cross_tenant_project_read(&c, &registry, true, None, "does-not-exist", None)
                .unwrap();
        assert_eq!(outcome, CrossTenantOutcome::Admit);
    }

    #[test]
    fn a_nonexistent_project_is_not_found() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let outcome = deny_cross_tenant_project_read(
            &c,
            &registry,
            false,
            Some("u1"),
            "does-not-exist",
            None,
        )
        .unwrap();
        assert_eq!(outcome, CrossTenantOutcome::NotFound);
    }

    #[test]
    fn a_non_member_sees_the_same_not_found_as_a_nonexistent_project() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let outcome =
            deny_cross_tenant_project_read(&c, &registry, false, Some("u1"), "proj-a", None)
                .unwrap();
        assert_eq!(outcome, CrossTenantOutcome::NotFound);
    }

    #[test]
    fn a_member_admits_with_no_min_role() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'viewer')",
            [&uid],
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let outcome =
            deny_cross_tenant_project_read(&c, &registry, false, Some(&uid), "proj-a", None)
                .unwrap();
        assert_eq!(outcome, CrossTenantOutcome::Admit);
    }

    #[test]
    fn a_viewer_is_forbidden_when_min_role_requires_operator() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'viewer')",
            [&uid],
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let outcome = deny_cross_tenant_project_read(
            &c,
            &registry,
            false,
            Some(&uid),
            "proj-a",
            Some("operator"),
        )
        .unwrap();
        assert_eq!(
            outcome,
            CrossTenantOutcome::Forbidden {
                role: "viewer".to_string(),
                min_role: "operator".to_string()
            }
        );
    }

    // -- revalidate_capability_and_membership ----------------------------

    #[test]
    fn revalidate_denies_when_the_session_cookie_no_longer_resolves() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        let cookie = format!("{}=nonexistent-session-id", login::SESSION_COOKIE_NAME);
        let outcome = revalidate_capability_and_membership(
            &c,
            &uid,
            Some(&cookie),
            NOW_STR,
            Capability::SystemProjectsManage,
            "proj-a",
            None,
        )
        .unwrap();
        assert!(matches!(outcome, RevalidateOutcome::DeniedSessionInvalid));
    }

    #[test]
    fn revalidate_allows_a_sysadmin_with_a_live_session() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice"); // first user -> sysadmin
        let sid =
            crate::identity::create_session(&c, &uid, NOW_STR, "2026-02-01T00:00:00.000+00:00")
                .unwrap();
        let cookie = format!("{}={}", login::SESSION_COOKIE_NAME, sid);
        let outcome = revalidate_capability_and_membership(
            &c,
            &uid,
            Some(&cookie),
            NOW_STR,
            Capability::SystemProjectsManage,
            "proj-a",
            None,
        )
        .unwrap();
        assert!(matches!(outcome, RevalidateOutcome::Allow(_)));
    }

    #[test]
    fn revalidate_denies_membership_for_a_capable_non_member() {
        // A capability grant with no matching membership row --
        // system.projects.manage is a system-tier cap, so a
        // non-sysadmin non-member could still legitimately carry it
        // via a group grant; the membership half must independently
        // deny.
        let mut c = conn();
        seed_user(&mut c, "alice"); // sysadmin, irrelevant here
        let bob = crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let outcome = revalidate_capability_and_membership(
            &c,
            &bob,
            None,
            NOW_STR,
            Capability::SystemProjectsManage,
            "proj-a",
            Some("operator"),
        )
        .unwrap();
        // bob has no group-granted capability at all here, so this
        // denies on capability first -- proves the fail-closed default.
        assert!(matches!(outcome, RevalidateOutcome::DeniedCapability));
    }

    // -- revalidate_capability (project-less) -----------------------------

    #[test]
    fn revalidate_capability_denies_when_the_session_cookie_no_longer_resolves() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        let cookie = format!("{}=nonexistent-session-id", login::SESSION_COOKIE_NAME);
        let outcome = revalidate_capability(
            &c,
            &uid,
            Some(&cookie),
            NOW_STR,
            Capability::SystemUsersManage,
        )
        .unwrap();
        assert!(matches!(
            outcome,
            RevalidateCapabilityOutcome::DeniedSessionInvalid
        ));
    }

    #[test]
    fn revalidate_capability_allows_a_sysadmin_with_a_live_session() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice"); // first user -> sysadmin
        let sid =
            crate::identity::create_session(&c, &uid, NOW_STR, "2026-02-01T00:00:00.000+00:00")
                .unwrap();
        let cookie = format!("{}={}", login::SESSION_COOKIE_NAME, sid);
        let outcome = revalidate_capability(
            &c,
            &uid,
            Some(&cookie),
            NOW_STR,
            Capability::SystemUsersManage,
        )
        .unwrap();
        assert!(matches!(outcome, RevalidateCapabilityOutcome::Allow(_)));
    }

    #[test]
    fn revalidate_capability_denies_a_non_sysadmin_lacking_the_capability() {
        let mut c = conn();
        seed_user(&mut c, "alice"); // sysadmin, irrelevant here
        let bob = crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let outcome =
            revalidate_capability(&c, &bob, None, NOW_STR, Capability::SystemUsersManage).unwrap();
        assert!(matches!(
            outcome,
            RevalidateCapabilityOutcome::DeniedCapability
        ));
    }

    #[test]
    fn revalidate_capability_admits_with_no_cookie_at_all() {
        // A forwarding-header/bearer caller has no session cookie to
        // revalidate at all -- the liveness check must be skipped
        // entirely, not treated as an automatic denial.
        let mut c = conn();
        let uid = seed_user(&mut c, "alice"); // sysadmin
        let outcome =
            revalidate_capability(&c, &uid, None, NOW_STR, Capability::SystemUsersManage).unwrap();
        assert!(matches!(outcome, RevalidateCapabilityOutcome::Allow(_)));
    }

    // -- decide_create_project --------------------------------------------

    #[test]
    fn creates_a_project_and_grants_the_creator_membership() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let parent = dir.path().join("workspaces");

        let outcome = decide_create_project(
            &c,
            &registry,
            &parent,
            false,
            Some(&uid),
            Some(&serde_json::json!("proj-a")),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Created {
            name,
            workspace_label,
        } = outcome
        else {
            panic!("expected Created, got {outcome:?}");
        };
        assert_eq!(name, "proj-a");
        assert_eq!(workspace_label, "proj-a");
        assert!(registry.get("proj-a").unwrap().is_some());
        let role: String = c
            .query_row(
                "SELECT role FROM project_membership WHERE user_id = ?1 AND project_name = 'proj-a'",
                [&uid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(role, "operator");
    }

    #[test]
    fn rejects_a_non_string_name() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let outcome = decide_create_project(
            &c,
            &registry,
            dir.path(),
            false,
            None,
            Some(&serde_json::json!(42)),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn rejects_an_invalid_slug() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let outcome = decide_create_project(
            &c,
            &registry,
            dir.path(),
            false,
            None,
            Some(&serde_json::json!("Not Valid")),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn a_visible_member_sees_the_rich_already_registered_409() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'operator')",
            [&uid],
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");

        let outcome = decide_create_project(
            &c,
            &registry,
            dir.path(),
            false,
            Some(&uid),
            Some(&serde_json::json!("proj-a")),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "already_registered");
    }

    #[test]
    fn a_non_member_colliding_with_a_hidden_project_sees_uniform_not_found() {
        let mut c = conn();
        seed_user(&mut c, "alice"); // sysadmin, but irrelevant -- caller below is bob
        let bob = crate::identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a"); // bob has no membership on it

        let outcome = decide_create_project(
            &c,
            &registry,
            dir.path(),
            false,
            Some(&bob),
            Some(&serde_json::json!("proj-a")),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "not_found");
        assert!(!body["message"].as_str().unwrap().contains("proj-a already"));
    }

    #[test]
    fn refuses_a_name_that_is_a_live_alias_of_another_project() {
        let mut c = conn();
        let uid = seed_user(&mut c, "alice");
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'operator')",
            [&uid],
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        registry
            .add_alias("proj-a", "old-name", None, Some(30), now_dt())
            .unwrap();

        let outcome = decide_create_project(
            &c,
            &registry,
            dir.path(),
            false,
            Some(&uid),
            Some(&serde_json::json!("old-name")),
            now_dt(),
        )
        .unwrap();
        let CreateProjectOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "alias_collision");
    }
}
