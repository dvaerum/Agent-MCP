//! Synchronous decision functions for `delete_project_handler`/
//! `stop_project_handler`. Port of the decision halves of
//! `admin_api.py` (Phase E2 PR 18, `conexus-router-project-teardown`).
//!
//! Both handlers share an IDENTICAL precheck shape (confirmed against
//! the real Python source, not assumed from the research summary
//! alone): the entry gate (`_deny_cross_tenant_project_read`,
//! `min_role="operator"`), a `not_registered` existence check (load-
//! bearing for a SYSADMIN caller, since the entry gate's own existence
//! probe is skipped for sysadmins), then an `active_conns` guard --
//! [`project_mutation_precheck`] is the ONE function both call.
//!
//! The actual `systemctl stop`/`is-active` AWAIT and the
//! `_ensure_lock` it runs inside are PR 23's job -- this crate's own
//! established deferral (see `project_gate.rs`'s module doc) for the
//! same reason: `session_gate.rs`/`project_gate.rs` already prove the
//! DB-side re-check needs no async, and inventing a generic async
//! fusion wrapper ahead of axum's real yield-point shape would be
//! guessing. [`finish_delete_project`]/[`finish_stop_project`] are the
//! synchronous mutation that runs AFTER that (deferred) await
//! resolves -- they take no systemctl result as input because neither
//! Python handler branches on it (a delete/stop's final state-clear is
//! unconditional either way, per BL-R36-1's own "even when the unit
//! was already inactive" fix).
#![allow(dead_code)]

use rusqlite::Connection;

use crate::lifecycle::{self, LifecycleError};
use crate::mcp_handler::HandlerResponse;
use crate::orchestrator::runtime::RuntimeStore;
use crate::project_gate::{deny_cross_tenant_project_read, CrossTenantOutcome, GateError};
use crate::project_registry::ProjectRegistry;

/// Port of `_app.active_conns.get(name, 0)` -- the live proxy-
/// connection count for `name`, read fresh (used both at entry and,
/// per BL-R6-1/R3-F3, again inside the lock as a TOCTOU re-check
/// immediately before the destructive stop).
pub fn active_connections(store: &RuntimeStore, name: &str) -> u32 {
    store.snapshot(name).map(|rt| rt.active_conns).unwrap_or(0)
}

#[derive(Debug)]
pub enum MutationPrecheck {
    Proceed,
    Rejected(HandlerResponse),
}

/// The shared precheck `delete_project_handler`/`stop_project_handler`
/// both run, byte-for-byte identical in the real Python source: the
/// cross-tenant entry gate (operator-tier membership required), a
/// `not_registered` check (unreachable for a real non-sysadmin caller,
/// since the entry gate already closed that oracle -- but load-bearing
/// for a sysadmin, who bypasses the entry gate's existence probe
/// entirely), then the `active_conns` guard.
pub fn project_mutation_precheck(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    name: &str,
) -> Result<MutationPrecheck, GateError> {
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
            return Ok(MutationPrecheck::Rejected(lifecycle::error_envelope(
                LifecycleError::NotFound,
                &format!("unknown project: {name:?}"),
                None,
            )));
        }
        CrossTenantOutcome::Forbidden { role, min_role } => {
            return Ok(MutationPrecheck::Rejected(lifecycle::error_envelope(
                LifecycleError::Forbidden,
                &format!(
                    "operator holds only {role:?} membership on project {name:?}; \
                     this action requires at least {min_role:?}-tier membership"
                ),
                None,
            )));
        }
    }
    if registry.get(name)?.is_none() {
        return Ok(MutationPrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::NotRegistered,
            &format!("unknown project: {name:?}"),
            None,
        )));
    }
    let conns = active_connections(store, name);
    if conns > 0 {
        return Ok(MutationPrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::ActiveSessions,
            &format!("{name:?} has {conns} active connection(s); disconnect them and retry"),
            Some(serde_json::json!({"active_connections": conns, "agents": []})),
        )));
    }
    Ok(MutationPrecheck::Proceed)
}

/// Port of `delete_project_handler`'s post-`systemctl stop` mutation:
/// unregister (a no-op if already gone -- `ProjectRegistry::unregister`
/// is idempotent), clear runtime state (`keep_lock: true` -- the
/// caller pops its own `ensure_locks` entry once the surrounding lock
/// is released, matching Python's `ensure_locks.pop((name, "backend"),
/// None)` running AFTER the `async with` block exits), and best-effort
/// purge the project's `project_membership` rows (AZ-R13-1 parity --
/// never fails the delete itself).
pub fn finish_delete_project(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    name: &str,
) -> Result<(), GateError> {
    registry.unregister(name)?;
    store.forget(name, false, true);
    let _ = crate::identity::remove_project_membership_by_project(conn, name);
    Ok(())
}

/// Port of `stop_project_handler`'s post-`systemctl stop` mutation:
/// clear runtime state ONLY -- the project still exists afterward, so
/// (unlike delete) the registry/workspace/token/membership are
/// untouched. Unconditional, even when the unit turned out to already
/// be inactive (BL-R36-1: a stale `last_active` otherwise survives a
/// stop and corrupts the overview's `status`/`last_activity_ts`).
pub fn finish_stop_project(store: &RuntimeStore, name: &str) {
    store.forget(name, false, true);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
    use crate::mcp_handler::HandlerBody;
    use chrono::{DateTime, Utc};
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

    fn registry_with(dir: &std::path::Path, name: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        registry
            .register(name, "/ws/proj-a", "python", now_dt())
            .unwrap();
        registry
    }

    fn seed_operator_member(c: &mut Connection, project: &str, role: &str) -> String {
        let uid = identity::create_user(
            c,
            "bob",
            "correct horse battery staple",
            None,
            false,
            true, // first user -> sysadmin; harmless, membership below is what's tested
            &[],
            NOW_STR,
        )
        .unwrap();
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES (?1, ?2, ?3)",
            (project, &uid, role),
        )
        .unwrap();
        uid
    }

    // -- project_mutation_precheck --------------------------------------

    #[test]
    fn precheck_denies_a_nonexistent_project_as_uniform_not_found() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        let outcome =
            project_mutation_precheck(&c, &registry, &store, false, Some("u1"), "does-not-exist")
                .unwrap();
        let MutationPrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "not_found");
    }

    #[test]
    fn precheck_denies_a_non_member_as_the_same_uniform_not_found() {
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
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let store = RuntimeStore::default();
        let outcome =
            project_mutation_precheck(&c, &registry, &store, false, Some("nobody"), "proj-a")
                .unwrap();
        let MutationPrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn precheck_denies_a_viewer_tier_member_as_forbidden() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let uid = seed_operator_member(&mut c, "proj-a", "viewer");
        let store = RuntimeStore::default();
        let outcome =
            project_mutation_precheck(&c, &registry, &store, false, Some(&uid), "proj-a").unwrap();
        let MutationPrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "forbidden");
    }

    #[test]
    fn precheck_denies_when_the_project_has_active_connections() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let uid = seed_operator_member(&mut c, "proj-a", "operator");
        let store = RuntimeStore::default();
        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 2);

        let outcome =
            project_mutation_precheck(&c, &registry, &store, false, Some(&uid), "proj-a").unwrap();
        let MutationPrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "active_sessions");
        assert_eq!(body["active_connections"], 2);
    }

    #[test]
    fn precheck_proceeds_for_an_operator_member_with_no_active_connections() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let uid = seed_operator_member(&mut c, "proj-a", "operator");
        let store = RuntimeStore::default();

        let outcome =
            project_mutation_precheck(&c, &registry, &store, false, Some(&uid), "proj-a").unwrap();
        assert!(matches!(outcome, MutationPrecheck::Proceed));
    }

    #[test]
    fn precheck_proceeds_for_a_sysadmin_even_with_no_membership_row() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
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
        let store = RuntimeStore::default();

        let outcome =
            project_mutation_precheck(&c, &registry, &store, true, Some(&uid), "proj-a").unwrap();
        assert!(matches!(outcome, MutationPrecheck::Proceed));
    }

    #[test]
    fn precheck_denies_a_sysadmin_on_a_genuinely_nonexistent_project() {
        // A sysadmin skips the entry gate's own existence probe
        // entirely -- the SEPARATE not_registered check must catch it.
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        let outcome =
            project_mutation_precheck(&c, &registry, &store, true, Some("u1"), "does-not-exist")
                .unwrap();
        let MutationPrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "not_registered");
    }

    // -- finish_delete_project / finish_stop_project --------------------

    #[test]
    fn finish_delete_project_unregisters_clears_runtime_and_purges_membership() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let uid = seed_operator_member(&mut c, "proj-a", "operator");
        let store = RuntimeStore::default();
        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 0);
        store.ensure_lock("proj-a", "backend"); // simulate a held lock

        finish_delete_project(&c, &registry, &store, "proj-a").unwrap();

        assert!(registry.get("proj-a").unwrap().is_none());
        assert!(store.snapshot("proj-a").is_none());
        let count: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM project_membership WHERE project_name = 'proj-a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
        let _ = uid;
    }

    #[test]
    fn finish_delete_project_on_an_already_gone_project_is_a_noop() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        finish_delete_project(&c, &registry, &store, "never-existed").unwrap();
    }

    #[test]
    fn finish_stop_project_clears_runtime_but_leaves_the_registry_and_membership_untouched() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a");
        let uid = seed_operator_member(&mut c, "proj-a", "operator");
        let store = RuntimeStore::default();
        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 0);
        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times
                .insert("backend".to_string(), std::time::Instant::now());
        });

        finish_stop_project(&store, "proj-a");

        assert!(
            registry.get("proj-a").unwrap().is_some(),
            "stop must not unregister the project"
        );
        assert!(
            store.snapshot("proj-a").is_none(),
            "runtime state must be cleared"
        );
        let count: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM project_membership WHERE project_name = 'proj-a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "stop must not touch project_membership");
        let _ = uid;
    }
}
