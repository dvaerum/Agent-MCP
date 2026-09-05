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
//! `_ensure_lock` it runs inside were PR 23's job -- wired for real
//! in `lifecycle_rest.rs` (step 6c), using `perm_gates::
//! revalidated_lock`/`revalidate_after` fused around
//! `orchestrator::primitives::systemctl`/`is_active`.
//! [`finish_delete_project`]/[`finish_stop_project`] are the
//! synchronous mutation that runs AFTER that await resolves --
//! **corrected from an earlier claim in this doc**: delete's stop
//! result IS ignored (BL-R36-1: the unregister/purge proceeds
//! unconditionally, even when the unit was already inactive or the
//! stop itself failed), but stop's own handler DOES branch on it (a
//! nonzero `systemctl stop` return code is a 500, and
//! `finish_stop_project` is never called in that case) -- the two
//! handlers are NOT symmetric here, confirmed by re-reading the real
//! Python source, not assumed from the file's own earlier framing.
#![allow(dead_code)]

use std::path::Path;

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

/// Port of the shared "conns > 0" 409 -- extracted once a SECOND real
/// call site needed the identical shape: `project_mutation_precheck`'s
/// own entry-time probe, and [`active_sessions_recheck`]'s in-lock
/// TOCTOU re-check (gap 8 from PR23 step 6's own research -- Python
/// hand-duplicates this exact branch at both call sites; a Rust
/// handler author gets it once instead).
fn active_sessions_response(store: &RuntimeStore, name: &str) -> Option<HandlerResponse> {
    let conns = active_connections(store, name);
    if conns > 0 {
        Some(lifecycle::error_envelope(
            LifecycleError::ActiveSessions,
            &format!("{name:?} has {conns} active connection(s); disconnect them and retry"),
            Some(serde_json::json!({"active_connections": conns, "agents": []})),
        ))
    } else {
        None
    }
}

/// The IN-LOCK re-check both `delete_project_handler`/
/// `stop_project_handler` run immediately before their destructive
/// `systemctl stop` (R3-F3/BL-R6-1: the entry-time probe above ran
/// OUTSIDE the lock and is never re-checked on its own -- a client
/// whose stream starts connecting in the window between that check
/// and the actual stop must still get the clean 409 rather than
/// racing the teardown).
pub fn active_sessions_recheck(store: &RuntimeStore, name: &str) -> Option<HandlerResponse> {
    active_sessions_response(store, name)
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
    if let Some(resp) = active_sessions_response(store, name) {
        return Ok(MutationPrecheck::Rejected(resp));
    }
    Ok(MutationPrecheck::Proceed)
}

/// Port of `?delete_workspace=true`'s truthy-string parsing
/// (`{"true","1","yes","on"}`, case-insensitive) -- `delete_project_
/// handler`'s own opt-in for recursive workspace removal.
pub fn parse_delete_workspace_flag(raw: Option<&str>) -> bool {
    matches!(
        raw.map(|s| s.to_ascii_lowercase()).as_deref(),
        Some("true") | Some("1") | Some("yes") | Some("on")
    )
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceDeleteOutcome {
    pub deleted: bool,
    pub skipped_reason: Option<String>,
}

/// Port of `delete_project_handler`'s `?delete_workspace=true` opt-in
/// recursive workspace removal. Runs BEFORE the lock, exactly like
/// Python -- a standalone, synchronous mutation genuinely separate
/// from the stop+unregister the lock protects (moving it inside would
/// change no safety property Python's own placement cares about).
///
/// SD-R15: `skipped_reason` must never carry the resolved ABSOLUTE
/// workspace path (server home dir / deployment filesystem layout) --
/// only a generic category. `std::io::Error`'s own `Display` already
/// omits the path for a bare `remove_dir_all` failure (unlike some of
/// Python's `OSError` variants), so no extra scrubbing is needed here.
pub fn maybe_delete_workspace(
    workspace_path: &Path,
    default_workspace_parent: &Path,
    want_delete: bool,
) -> WorkspaceDeleteOutcome {
    if !want_delete {
        return WorkspaceDeleteOutcome {
            deleted: false,
            skipped_reason: None,
        };
    }
    if !lifecycle::is_within_default_workspace(workspace_path, default_workspace_parent) {
        return WorkspaceDeleteOutcome {
            deleted: false,
            skipped_reason: Some(
                "workspace resolves outside the default workspace parent; refusing recursive delete"
                    .to_string(),
            ),
        };
    }
    if workspace_path.exists() {
        match std::fs::remove_dir_all(workspace_path) {
            Ok(()) => WorkspaceDeleteOutcome {
                deleted: true,
                skipped_reason: None,
            },
            Err(e) => WorkspaceDeleteOutcome {
                deleted: false,
                skipped_reason: Some(format!("could not delete project workspace: {e}")),
            },
        }
    } else {
        WorkspaceDeleteOutcome {
            deleted: true,
            skipped_reason: Some("workspace did not exist on disk".to_string()),
        }
    }
}

/// Port of `delete_project_handler`'s post-`systemctl stop` mutation:
/// unregister (a no-op if already gone -- `ProjectRegistry::unregister`
/// is idempotent), clear runtime state (`keep_lock: true` -- the
/// caller pops its own `ensure_locks` entry once the surrounding lock
/// is released, matching Python's `ensure_locks.pop((name, "backend"),
/// None)` running AFTER the `async with` block exits), best-effort
/// purge the project's `project_membership` rows (AZ-R13-1 parity --
/// never fails the delete itself), best-effort delete the project's
/// token files (`admin_api.py:1000-1005`), and best-effort purge the
/// per-project runtime dir under `sock_dir` (`admin_api.py:1013-1023`,
/// SC-3: `RuntimeDirectoryPreserve=yes` means systemd won't do this
/// for us on a delete). **Found-and-fixed bug**: this function
/// originally did NEITHER purge, an asymmetry with
/// `project_rename::finish_rename_project`'s equivalent (correct)
/// purge from the SAME phase, not a documented scope decision.
pub fn finish_delete_project(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    name: &str,
    sock_dir: &Path,
    token_dir: Option<&Path>,
) -> Result<(), GateError> {
    registry.unregister(name)?;
    store.forget(name, false, true);
    let _ = crate::identity::remove_project_membership_by_project(conn, name);
    purge_token_files(token_dir, name);
    if lifecycle::SLUG_RE.is_match(name) {
        let runtime_dir = sock_dir.join(name);
        if runtime_dir.is_dir() {
            let _ = std::fs::remove_dir_all(&runtime_dir);
        }
    }
    Ok(())
}

/// Port of the `token_dir.glob(f"{name}--*.token")` unlink loop
/// (`admin_api.py:1000-1005`) -- unlike rename's sibling
/// `rename_token_files` (which renames the matched files), a delete
/// unlinks them outright. Best-effort: an absent `token_dir` or a
/// per-file `unlink` failure is silently ignored, matching Python's
/// own `except OSError: pass`.
fn purge_token_files(token_dir: Option<&Path>, name: &str) {
    let Some(dir) = token_dir else { return };
    if !dir.is_dir() {
        return;
    }
    let prefix = format!("{name}--");
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let fname = entry.file_name();
        let fname_str = fname.to_string_lossy();
        if fname_str.starts_with(&prefix) && fname_str.ends_with(".token") {
            let _ = std::fs::remove_file(entry.path());
        }
    }
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

    // -- parse_delete_workspace_flag ------------------------------------

    #[test]
    fn parse_delete_workspace_flag_accepts_the_documented_truthy_strings() {
        for s in ["true", "TRUE", "1", "yes", "YES", "on"] {
            assert!(
                parse_delete_workspace_flag(Some(s)),
                "{s:?} should be truthy"
            );
        }
    }

    #[test]
    fn parse_delete_workspace_flag_rejects_everything_else() {
        for s in [None, Some(""), Some("false"), Some("0"), Some("no")] {
            assert!(!parse_delete_workspace_flag(s), "{s:?} should be falsy");
        }
    }

    // -- maybe_delete_workspace ------------------------------------------

    #[test]
    fn maybe_delete_workspace_is_a_noop_when_not_requested() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("parent").join("proj-a");
        std::fs::create_dir_all(&ws).unwrap();
        let outcome = maybe_delete_workspace(&ws, &dir.path().join("parent"), false);
        assert_eq!(
            outcome,
            WorkspaceDeleteOutcome {
                deleted: false,
                skipped_reason: None
            }
        );
        assert!(ws.exists(), "a non-requested delete must not touch the dir");
    }

    #[test]
    fn maybe_delete_workspace_refuses_a_workspace_outside_the_default_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("parent");
        let outside = dir.path().join("elsewhere").join("proj-a");
        std::fs::create_dir_all(&outside).unwrap();
        let outcome = maybe_delete_workspace(&outside, &parent, true);
        assert!(!outcome.deleted);
        assert_eq!(
            outcome.skipped_reason.as_deref(),
            Some(
                "workspace resolves outside the default workspace parent; refusing recursive delete"
            )
        );
        assert!(outside.exists(), "refused delete must not touch the dir");
        assert!(
            !outcome
                .skipped_reason
                .unwrap()
                .contains(&outside.to_string_lossy().to_string()),
            "SD-R15: skipped_reason must never leak the absolute path"
        );
    }

    #[test]
    fn maybe_delete_workspace_recursively_removes_a_real_workspace_within_the_parent() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("parent");
        let ws = parent.join("proj-a");
        std::fs::create_dir_all(ws.join("nested")).unwrap();
        std::fs::write(ws.join("nested").join("file.txt"), b"data").unwrap();

        let outcome = maybe_delete_workspace(&ws, &parent, true);

        assert_eq!(
            outcome,
            WorkspaceDeleteOutcome {
                deleted: true,
                skipped_reason: None
            }
        );
        assert!(!ws.exists());
    }

    #[test]
    fn maybe_delete_workspace_treats_an_already_absent_workspace_as_deleted() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join("parent");
        std::fs::create_dir_all(&parent).unwrap();
        let ws = parent.join("proj-a"); // never created

        let outcome = maybe_delete_workspace(&ws, &parent, true);

        assert!(outcome.deleted);
        assert_eq!(
            outcome.skipped_reason.as_deref(),
            Some("workspace did not exist on disk")
        );
    }

    // -- active_sessions_recheck -------------------------------------------

    #[test]
    fn active_sessions_recheck_is_none_when_the_project_is_idle() {
        let store = RuntimeStore::default();
        assert!(active_sessions_recheck(&store, "proj-a").is_none());
    }

    #[test]
    fn active_sessions_recheck_matches_the_entry_time_409_shape() {
        let store = RuntimeStore::default();
        store.with_runtime_mut("proj-a", |rt| rt.active_conns = 3);
        let resp = active_sessions_recheck(&store, "proj-a").unwrap();
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON");
        };
        assert_eq!(body["error"], "active_sessions");
        assert_eq!(body["active_connections"], 3);
    }

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

        finish_delete_project(
            &c,
            &registry,
            &store,
            "proj-a",
            &dir.path().join("sock"),
            None,
        )
        .unwrap();

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
        finish_delete_project(
            &c,
            &registry,
            &store,
            "never-existed",
            &dir.path().join("sock"),
            None,
        )
        .unwrap();
    }

    // -- found-bug regression: token-file + runtime-dir purge -----------

    #[test]
    fn finish_delete_project_purges_matching_token_files_but_leaves_others() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        let token_dir = dir.path().join("tokens");
        std::fs::create_dir_all(&token_dir).unwrap();
        std::fs::write(token_dir.join("proj-a--worker1.token"), b"secret").unwrap();
        std::fs::write(token_dir.join("proj-a--worker2.token"), b"secret").unwrap();
        std::fs::write(token_dir.join("proj-b--worker1.token"), b"secret").unwrap();

        finish_delete_project(
            &c,
            &registry,
            &store,
            "proj-a",
            &dir.path().join("sock"),
            Some(&token_dir),
        )
        .unwrap();

        assert!(!token_dir.join("proj-a--worker1.token").exists());
        assert!(!token_dir.join("proj-a--worker2.token").exists());
        assert!(
            token_dir.join("proj-b--worker1.token").exists(),
            "a different project's token must survive"
        );
    }

    #[test]
    fn finish_delete_project_purges_the_runtime_dir_under_sock_dir() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        let sock_dir = dir.path().join("sock");
        let runtime_dir = sock_dir.join("proj-a");
        std::fs::create_dir_all(&runtime_dir).unwrap();
        std::fs::write(runtime_dir.join("backend.sock"), b"").unwrap();
        std::fs::write(runtime_dir.join("forwarding_hmac"), b"key").unwrap();

        finish_delete_project(&c, &registry, &store, "proj-a", &sock_dir, None).unwrap();

        assert!(
            !runtime_dir.exists(),
            "the per-project runtime dir must be purged on delete"
        );
    }

    #[test]
    fn finish_delete_project_is_a_noop_when_neither_token_dir_nor_runtime_dir_exist() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();
        // No token_dir, no pre-existing sock_dir/proj-a -- must not error.
        finish_delete_project(
            &c,
            &registry,
            &store,
            "proj-a",
            &dir.path().join("sock"),
            Some(&dir.path().join("nonexistent-tokens")),
        )
        .unwrap();
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
