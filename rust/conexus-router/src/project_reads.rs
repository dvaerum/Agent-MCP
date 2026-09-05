//! Read-only decision functions for `admin_api.py`'s remaining
//! project-lifecycle surface: `health_handler`/`list_projects_handler`
//! (thin), plus the cross-DB read infrastructure `overview_handler`/
//! `alias_usage_handler` need -- genuinely new to this crate, since
//! every existing `conexus-db` repository is scoped to one already-
//! known connection, never an arbitrary per-project SQLite file opened
//! by absolute path at request time. Phase E2 PR 20,
//! `conexus-router-lifecycle-reads`.
//!
//! **Scoped OUT of this PR, deliberately**: `overview_handler`'s full
//! envelope (`_build_overview_envelope`) also needs `_po._is_active`
//! (a real `systemctl is-active` await) and `_derive_status`'s
//! running/last-activity fusion -- the async yield point this crate's
//! own established pattern defers to PR 23 (see `project_gate.rs`'s
//! module doc). [`project_counts`] is the fully synchronous half of
//! that envelope (three `COUNT` queries against the project's own
//! SQLite) and is complete here; assembling it alongside a real
//! `is_active` result into the full overview JSON is PR 23's job.
#![allow(dead_code)]

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

use chrono::{DateTime, Utc};
use conexus_db::group_membership_repository;
use rusqlite::{Connection, OpenFlags};

use crate::lifecycle::{self, LifecycleError};
use crate::mcp_handler::{HandlerBody, HandlerResponse};
use crate::project_gate::{deny_cross_tenant_project_read, CrossTenantOutcome, GateError};
use crate::project_registry::ProjectRegistry;
use crate::single_tenant::bypasses_operator_gate;

/// Port of `health_handler`'s public service descriptor. Deliberately
/// never echoes the deployed package version (SEC, owner-authorised) --
/// an unauthenticated liveness probe learns only "the router is up".
pub fn health_response(single_tenant_name: Option<&str>) -> HandlerResponse {
    HandlerResponse {
        status: 200,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(serde_json::json!({
            "ok": true,
            "service": "agent-mcp-router",
            "mode": if single_tenant_name.is_some() { "single-tenant" } else { "multi-tenant" },
        })),
    }
}

/// Port of `_visible_project_names` (SEC FINDING 4): the subset of
/// `names` the caller may see -- single-tenant/sysadmin see
/// everything; otherwise filtered to a resolved `project_membership`
/// role (direct or via a group, same access model the session gate
/// itself uses). A DB error on one name fails closed (that name is
/// simply omitted, matching Python's `except: continue`) rather than
/// over-disclosing or failing the whole listing.
pub fn visible_project_names(
    conn: &Connection,
    single_tenant_name: Option<&str>,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    names: &[String],
) -> HashSet<String> {
    if bypasses_operator_gate(single_tenant_name) || is_sysadmin {
        return names.iter().cloned().collect();
    }
    let Some(user_id) = caller_user_id else {
        return HashSet::new();
    };
    names
        .iter()
        .filter(|name| {
            group_membership_repository::resolve_user_project_role(conn, user_id, name, None)
                .ok()
                .flatten()
                .is_some()
        })
        .cloned()
        .collect()
}

/// Port of `list_projects_handler`.
pub fn list_projects_response(
    conn: &Connection,
    registry: &ProjectRegistry,
    single_tenant_name: Option<&str>,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
) -> Result<HandlerResponse, GateError> {
    let mut names: Vec<String> = registry.list()?.into_iter().map(|p| p.name).collect();
    names.sort();
    let visible = visible_project_names(
        conn,
        single_tenant_name,
        is_sysadmin,
        caller_user_id,
        &names,
    );
    let filtered: Vec<&String> = names.iter().filter(|n| visible.contains(*n)).collect();
    Ok(HandlerResponse {
        status: 200,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(serde_json::json!({ "projects": filtered })),
    })
}

/// Port of `_project_db_path`: the per-project SQLite always lives at
/// `<workspace>/.agent/mcp_state.db`.
pub fn project_db_path(workspace: &str) -> PathBuf {
    Path::new(workspace).join(".agent").join("mcp_state.db")
}

/// A read-only connection to a per-project SQLite file, or `None` when
/// the file doesn't exist / can't be opened -- every caller degrades
/// to a zero/empty result on `None`, matching Python's own "never
/// raise, render zeros" contract for a project that hasn't been
/// touched yet.
fn open_readonly(db_path: &Path) -> Option<Connection> {
    if !db_path.is_file() {
        return None;
    }
    let uri = format!("file:{}?mode=ro", db_path.display());
    let conn = Connection::open_with_flags(
        uri,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .ok()?;
    let _ = conn.busy_timeout(Duration::from_secs(1));
    Some(conn)
}

fn count(conn: &Connection, sql: &str) -> i64 {
    conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap_or(0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ProjectCounts {
    pub agents: i64,
    pub tasks: i64,
    pub open_messages: i64,
}

/// Port of `_project_counts`: three independent `COUNT` queries
/// against the project's own SQLite, opened read-only (`mode=ro` --
/// never contends with the backend's own write lock, never risks a
/// write from the router). A missing DB file, a missing table (a
/// freshly-registered project, or a half-migrated one), or any other
/// SQLite error degrades that ONE count to zero rather than failing
/// the whole card -- matching Python's per-query `try/except`
/// granularity exactly (one broken table doesn't zero the other two).
pub fn project_counts(workspace: &str) -> ProjectCounts {
    let db = project_db_path(workspace);
    let Some(conn) = open_readonly(&db) else {
        return ProjectCounts::default();
    };
    ProjectCounts {
        agents: count(&conn, "SELECT COUNT(*) FROM agents"),
        tasks: count(&conn, "SELECT COUNT(*) FROM tasks"),
        open_messages: count(&conn, "SELECT COUNT(*) FROM agent_messages WHERE read = 0"),
    }
}

/// Port of `alias_usage_handler`'s DB read: every distinct `agent_id`
/// that has used `alias` against this project's own `mcp_sessions`
/// table. Degrades to an empty list on any error (missing DB, missing
/// table, ...), matching Python's `except: logger.exception(...)`
/// fallback -- a query failure never fails the whole response.
pub fn alias_usage_agents(workspace: &str, alias: &str) -> Vec<String> {
    let db = project_db_path(workspace);
    let Some(conn) = open_readonly(&db) else {
        return Vec::new();
    };
    let result: rusqlite::Result<Vec<String>> = (|| {
        let mut stmt = conn.prepare(
            "SELECT DISTINCT agent_id FROM mcp_sessions WHERE alias_used = ?1 AND agent_id IS NOT NULL",
        )?;
        let rows = stmt.query_map([alias], |r| r.get::<_, String>(0))?;
        rows.collect()
    })();
    result.unwrap_or_default()
}

#[derive(Debug)]
pub enum AliasUsageOutcome {
    Found {
        alias: String,
        project: String,
        expires_at: String,
        agents: Vec<String>,
    },
    Rejected(HandlerResponse),
}

/// A plain 404/400 with no JSON envelope -- port of Python's bare
/// `web.HTTPNotFound(reason=...)`/`web.HTTPBadRequest(reason=...)`
/// (distinct from `lifecycle::error_envelope`'s `{success, error,
/// message}` shape, which this handler deliberately does NOT use for
/// its own alias-specific not-found cases). The `reason` phrase itself
/// has no `HandlerResponse` field to carry it (HTTP/1.1 status-line
/// reason phrases are cosmetic and widely ignored by modern clients);
/// PR 23's real axum layer may attach one directly if ever needed.
fn plain_status(status: u16) -> HandlerResponse {
    HandlerResponse {
        status,
        headers: vec![],
        body: HandlerBody::Empty,
    }
}

/// Port of `alias_usage_handler`'s full decision: the entry gate
/// (R4-F3, membership-scoped, no `min_role`), the required `alias`
/// query param, alias resolution (fixed reason phrases throughout --
/// never reflects the caller-supplied alias/project name, SEC4
/// pattern), and the path-vs-resolved-owner consistency check.
pub fn decide_alias_usage(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    project_name: &str,
    raw_alias: &str,
    now: DateTime<Utc>,
) -> Result<AliasUsageOutcome, GateError> {
    match deny_cross_tenant_project_read(
        conn,
        registry,
        is_sysadmin,
        caller_user_id,
        project_name,
        None,
    )? {
        CrossTenantOutcome::Admit => {}
        CrossTenantOutcome::NotFound => {
            return Ok(AliasUsageOutcome::Rejected(lifecycle::error_envelope(
                LifecycleError::NotFound,
                &format!("unknown project: {project_name:?}"),
                None,
            )));
        }
        CrossTenantOutcome::Forbidden { .. } => {
            // Unreachable in practice: `min_role: None` never produces
            // Forbidden (see `deny_cross_tenant_project_read`'s own
            // doc) -- kept exhaustive rather than `unreachable!()` so a
            // future change to that contract fails a compile check
            // here, not a runtime panic.
            return Ok(AliasUsageOutcome::Rejected(plain_status(403)));
        }
    }

    let alias = raw_alias.trim();
    if alias.is_empty() {
        return Ok(AliasUsageOutcome::Rejected(plain_status(400)));
    }
    let Some(real_name) = registry.resolve_alias(alias, now)? else {
        return Ok(AliasUsageOutcome::Rejected(plain_status(404)));
    };
    if real_name != project_name {
        // The path-keyed project doesn't own this alias -- same UX as
        // "alias not found here".
        return Ok(AliasUsageOutcome::Rejected(plain_status(404)));
    }
    let Some(row) = registry.get(&real_name)? else {
        return Ok(AliasUsageOutcome::Rejected(plain_status(404)));
    };
    let expires_at = row
        .aliases
        .iter()
        .find(|a| a.name == alias)
        .map(|a| a.expires_at.clone())
        .unwrap_or_default();
    let agents = alias_usage_agents(&row.workspace, alias);
    Ok(AliasUsageOutcome::Found {
        alias: alias.to_string(),
        project: real_name,
        expires_at,
        agents,
    })
}

#[derive(Debug)]
pub enum RemoveAliasOutcome {
    Removed(HandlerResponse),
    Rejected(HandlerResponse),
}

/// Port of `remove_alias_handler`'s full decision (PR23 step 6, gap
/// 10 -- no prior Rust coverage at all). Unlike the read-only
/// `alias_usage_handler` above, this is a MUTATION: `min_role:
/// Some("operator")`, mirroring rename/delete/stop (a mere
/// `viewer`-tier member must not expire an alias). `expire_alias` is
/// idempotent (a no-op on an already-gone alias or project), matching
/// Python's own `_REGISTRY.expire_alias` -- there is no separate
/// "alias not found" branch to port; only the project's own
/// existence is checked. The success body is a BARE JSON object
/// (`{"removed", "project", "remaining_aliases"}`, no `"success"`
/// wrapper) -- Python's real handler calls `web.json_response`
/// directly here, not `_success_envelope`, matching
/// `alias_usage_handler`'s own bare-response precedent.
pub fn decide_remove_alias(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    name: &str,
    alias: &str,
) -> Result<RemoveAliasOutcome, GateError> {
    match deny_cross_tenant_project_read(
        conn,
        registry,
        is_sysadmin,
        caller_user_id,
        name,
        Some("operator"),
    )? {
        CrossTenantOutcome::Admit => {}
        CrossTenantOutcome::NotFound => {
            return Ok(RemoveAliasOutcome::Rejected(plain_status(404)));
        }
        CrossTenantOutcome::Forbidden { .. } => {
            return Ok(RemoveAliasOutcome::Rejected(plain_status(403)));
        }
    }
    if registry.get(name)?.is_none() {
        return Ok(RemoveAliasOutcome::Rejected(plain_status(404)));
    }
    registry.expire_alias(name, alias)?;
    let remaining: Vec<serde_json::Value> = registry
        .get(name)?
        .map(|row| {
            row.aliases
                .into_iter()
                .map(|a| serde_json::json!({"name": a.name, "expires_at": a.expires_at}))
                .collect()
        })
        .unwrap_or_default();
    Ok(RemoveAliasOutcome::Removed(HandlerResponse {
        status: 200,
        headers: vec![("Cache-Control".to_string(), "no-store".to_string())],
        body: HandlerBody::Json(serde_json::json!({
            "removed": alias,
            "project": name,
            "remaining_aliases": remaining,
        })),
    }))
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

    fn now_dt() -> DateTime<Utc> {
        "2026-01-01T00:00:00Z".parse().unwrap()
    }
    const NOW_STR: &str = "2026-01-01T00:00:00.000+00:00";

    // -- health_response --------------------------------------------------

    #[test]
    fn health_response_reports_multi_tenant_by_default() {
        let resp = health_response(None);
        assert_eq!(resp.status, 200);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["ok"], true);
        assert_eq!(body["service"], "agent-mcp-router");
        assert_eq!(body["mode"], "multi-tenant");
    }

    #[test]
    fn health_response_reports_single_tenant_when_configured() {
        let resp = health_response(Some("demo"));
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["mode"], "single-tenant");
    }

    // -- visible_project_names / list_projects_response --------------------

    #[test]
    fn visible_project_names_sees_everything_in_single_tenant_mode() {
        let c = conn();
        let names = vec!["a".to_string(), "b".to_string()];
        let visible = visible_project_names(&c, Some("a"), false, None, &names);
        assert_eq!(visible, names.into_iter().collect());
    }

    #[test]
    fn visible_project_names_sees_everything_for_a_sysadmin() {
        let c = conn();
        let names = vec!["a".to_string(), "b".to_string()];
        let visible = visible_project_names(&c, None, true, Some("u1"), &names);
        assert_eq!(visible, names.into_iter().collect());
    }

    #[test]
    fn visible_project_names_filters_to_resolved_membership() {
        let mut c = conn();
        let uid = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('a', ?1, 'viewer')",
            [&uid],
        )
        .unwrap();
        let names = vec!["a".to_string(), "b".to_string()];
        let visible = visible_project_names(&c, None, false, Some(&uid), &names);
        assert_eq!(visible, ["a".to_string()].into_iter().collect());
    }

    #[test]
    fn visible_project_names_is_empty_with_no_caller_identity() {
        let c = conn();
        let names = vec!["a".to_string()];
        let visible = visible_project_names(&c, None, false, None, &names);
        assert!(visible.is_empty());
    }

    #[test]
    fn list_projects_response_filters_by_visibility_and_sorts() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("zeta", "/ws/zeta", "python", now_dt())
            .unwrap();
        registry
            .register("alpha", "/ws/alpha", "python", now_dt())
            .unwrap();
        let uid = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('alpha', ?1, 'viewer')",
            [&uid],
        )
        .unwrap();

        let resp = list_projects_response(&c, &registry, None, false, Some(&uid)).unwrap();
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["projects"], serde_json::json!(["alpha"]));
    }

    // -- project_counts / alias_usage_agents (real on-disk SQLite) -----------

    fn seed_project_db(dir: &Path) -> String {
        let agent_dir = dir.join(".agent");
        std::fs::create_dir_all(&agent_dir).unwrap();
        let db_path = agent_dir.join("mcp_state.db");
        let db = Connection::open(&db_path).unwrap();
        db.execute_batch(
            "CREATE TABLE agents (agent_id TEXT PRIMARY KEY);
             CREATE TABLE tasks (task_id TEXT PRIMARY KEY);
             CREATE TABLE agent_messages (id INTEGER PRIMARY KEY, read INTEGER NOT NULL);
             CREATE TABLE mcp_sessions (agent_id TEXT, alias_used TEXT);
             INSERT INTO agents VALUES ('a1'), ('a2');
             INSERT INTO tasks VALUES ('t1');
             INSERT INTO agent_messages (read) VALUES (0), (0), (1);
             INSERT INTO mcp_sessions (agent_id, alias_used) VALUES ('a1', 'old-name'), ('a2', 'old-name'), ('a1', 'old-name');",
        )
        .unwrap();
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn project_counts_reads_real_counts_from_a_real_sqlite_file() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = seed_project_db(dir.path());
        let counts = project_counts(&workspace);
        assert_eq!(counts.agents, 2);
        assert_eq!(counts.tasks, 1);
        assert_eq!(counts.open_messages, 2);
    }

    #[test]
    fn project_counts_degrades_to_zeros_when_the_db_is_missing() {
        let dir = tempfile::tempdir().unwrap();
        let counts = project_counts(dir.path().to_str().unwrap());
        assert_eq!(counts, ProjectCounts::default());
    }

    #[test]
    fn project_counts_degrades_one_missing_table_without_zeroing_the_others() {
        let dir = tempfile::tempdir().unwrap();
        let agent_dir = dir.path().join(".agent");
        std::fs::create_dir_all(&agent_dir).unwrap();
        let db = Connection::open(agent_dir.join("mcp_state.db")).unwrap();
        db.execute_batch(
            "CREATE TABLE agents (agent_id TEXT PRIMARY KEY); INSERT INTO agents VALUES ('a1');",
        )
        .unwrap();
        let counts = project_counts(dir.path().to_str().unwrap());
        assert_eq!(counts.agents, 1);
        assert_eq!(counts.tasks, 0);
        assert_eq!(counts.open_messages, 0);
    }

    #[test]
    fn alias_usage_agents_returns_distinct_agent_ids() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = seed_project_db(dir.path());
        let mut agents = alias_usage_agents(&workspace, "old-name");
        agents.sort();
        assert_eq!(agents, vec!["a1".to_string(), "a2".to_string()]);
    }

    #[test]
    fn alias_usage_agents_is_empty_for_an_unused_alias() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = seed_project_db(dir.path());
        assert!(alias_usage_agents(&workspace, "never-used").is_empty());
    }

    // -- decide_alias_usage -------------------------------------------------

    fn registry_with(dir: &Path, name: &str, workspace: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        registry
            .register(name, workspace, "python", now_dt())
            .unwrap();
        registry
    }

    #[test]
    fn decide_alias_usage_returns_the_real_agent_roster() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = seed_project_db(&dir.path().join("ws"));
        let registry = registry_with(dir.path(), "proj-a", &workspace);
        registry
            .add_alias("proj-a", "old-name", None, Some(30), now_dt())
            .unwrap();
        let c = conn();

        let outcome =
            decide_alias_usage(&c, &registry, true, None, "proj-a", "old-name", now_dt()).unwrap();
        let AliasUsageOutcome::Found {
            alias,
            project,
            agents,
            ..
        } = outcome
        else {
            panic!("expected Found, got {outcome:?}");
        };
        assert_eq!(alias, "old-name");
        assert_eq!(project, "proj-a");
        let mut sorted = agents;
        sorted.sort();
        assert_eq!(sorted, vec!["a1".to_string(), "a2".to_string()]);
    }

    #[test]
    fn decide_alias_usage_rejects_a_missing_alias_param() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");
        let c = conn();
        let outcome =
            decide_alias_usage(&c, &registry, true, None, "proj-a", "  ", now_dt()).unwrap();
        let AliasUsageOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn decide_alias_usage_404s_an_alias_that_belongs_to_a_different_project() {
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("proj-a", "/ws/proj-a", "python", now_dt())
            .unwrap();
        registry
            .register("proj-b", "/ws/proj-b", "python", now_dt())
            .unwrap();
        registry
            .add_alias("proj-b", "old-name", None, Some(30), now_dt())
            .unwrap();
        let c = conn();

        // "old-name" really belongs to proj-b, but the caller asks
        // against proj-a's path.
        let outcome =
            decide_alias_usage(&c, &registry, true, None, "proj-a", "old-name", now_dt()).unwrap();
        let AliasUsageOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn decide_alias_usage_closes_the_oracle_for_a_non_member() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");
        let bob = identity::create_user(
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
        let outcome = decide_alias_usage(
            &c,
            &registry,
            false,
            Some(&bob),
            "proj-a",
            "old-name",
            now_dt(),
        )
        .unwrap();
        let AliasUsageOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    // -- decide_remove_alias ----------------------------------------------

    #[test]
    fn decide_remove_alias_expires_the_alias_and_lists_what_remains() {
        let mut c = conn();
        let uid = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");
        registry
            .add_alias("proj-a", "old-name", None, Some(30), now_dt())
            .unwrap();
        registry
            .add_alias("proj-a", "older-name", None, Some(30), now_dt())
            .unwrap();

        let outcome =
            decide_remove_alias(&c, &registry, true, Some(&uid), "proj-a", "old-name").unwrap();
        let RemoveAliasOutcome::Removed(resp) = outcome else {
            panic!("expected Removed");
        };
        assert_eq!(resp.status, 200);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["removed"], "old-name");
        assert_eq!(body["project"], "proj-a");
        let remaining = body["remaining_aliases"].as_array().unwrap();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0]["name"], "older-name");
    }

    #[test]
    fn decide_remove_alias_is_idempotent_on_an_already_gone_alias() {
        let mut c = conn();
        let uid = identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");

        let outcome =
            decide_remove_alias(&c, &registry, true, Some(&uid), "proj-a", "never-existed")
                .unwrap();
        let RemoveAliasOutcome::Removed(resp) = outcome else {
            panic!("expected Removed even for an absent alias -- matches expire_alias's own idempotent no-op");
        };
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert!(body["remaining_aliases"].as_array().unwrap().is_empty());
    }

    #[test]
    fn decide_remove_alias_404s_a_nonexistent_project() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let outcome = decide_remove_alias(
            &c,
            &registry,
            true,
            Some("u1"),
            "does-not-exist",
            "old-name",
        )
        .unwrap();
        let RemoveAliasOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn decide_remove_alias_closes_the_cross_tenant_oracle_for_a_non_member() {
        let mut c = conn();
        identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW_STR,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");

        let outcome =
            decide_remove_alias(&c, &registry, false, Some(&bob), "proj-a", "old-name").unwrap();
        let RemoveAliasOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn decide_remove_alias_denies_a_viewer_tier_member_as_forbidden() {
        let mut c = conn();
        identity::create_user(
            &mut c,
            "alice",
            "correct horse battery staple",
            None,
            false,
            true,
            &[],
            NOW_STR,
        )
        .unwrap();
        let bob = identity::create_user(
            &mut c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            false,
            &[],
            NOW_STR,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('proj-a', ?1, 'viewer')",
            [&bob],
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "/ws/proj-a");

        let outcome =
            decide_remove_alias(&c, &registry, false, Some(&bob), "proj-a", "old-name").unwrap();
        let RemoveAliasOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
    }
}
