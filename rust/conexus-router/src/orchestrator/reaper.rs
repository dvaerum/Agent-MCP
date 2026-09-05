//! The idle/alias-grace background sweep loops -- port of
//! `project_orchestrator.py`'s "Background loops" +
//! "Alias-grace reaper" + "Startup reconciliation" sections (lines
//! 910-1082, Phase E2 PR 6d, the LAST of the 4-way orchestrator
//! split). Each tick is a free function taking explicit `now`/config
//! -- no owned `tokio::spawn`-loop lives here (Python's own `while
//! True: await asyncio.sleep(N); await _tick()` wrapper is a thin
//! shell around each tick body too; the eventual app-wiring PR is what
//! actually has a runtime to schedule these against, matching this
//! crate's own "explicit input, no hidden scheduling" convention).
//!
//! **`reconcile_on_startup` gets a REAL FIX, not a preserved-parity
//! port** (Phase E2 PR 6 research decision 2): Python's version scans
//! ONLY `agent-mcp@*` units on `systemctl list-units`, so a
//! `backend_impl="rust"` project surviving a router restart is never
//! adopted into `last_active` and therefore never reaped by
//! `IDLE_SEC` -- a genuine resource leak, not a deliberate design
//! choice (nothing in Python's own tests exercises the rust-unit case
//! either way). This port scans BOTH `agent-mcp@*` and `conexus@*` in
//! one `list-units` call.

use std::time::{Duration, SystemTime};

use chrono::{DateTime, Utc};

use crate::orchestrator::primitives::{backend_impl_for, run_systemctl, unit_name, SystemctlMode};
use crate::orchestrator::runtime::RuntimeStore;
use crate::project_registry::{ProjectRegistry, DEFAULT_BACKEND_IMPL};
use conexus_db::scheduled_directive_repository::parse_flexible;

/// One pass of the idle-reaper logic: stop every `(name, role)` whose
/// last-active timestamp is older than `idle`.
///
/// BL-R6-1-style TOCTOU guard: the `systemctl stop` shell-out yields
/// across a real await, so a concurrent `ensure()` warm-start could
/// reactivate the backend (refreshing its `last_active`) WHILE the
/// stop is in flight. Only [`RuntimeStore::forget`] the entry if the
/// timestamp this pass decided to reap on is STILL the current one --
/// otherwise a now-live backend would silently fall out of tracking
/// and never be reaped again.
pub async fn reaper_tick(
    store: &RuntimeStore,
    registry: &ProjectRegistry,
    idle: Duration,
    now: SystemTime,
    systemctl_program: &str,
    systemctl_mode: SystemctlMode,
    systemctl_timeout: Duration,
) {
    for (name, rt) in store.snapshot_all() {
        for (role, ts) in rt.last_active {
            let Ok(elapsed) = now.duration_since(ts) else {
                continue; // ts is somehow after `now` -- not idle.
            };
            if elapsed <= idle {
                continue;
            }
            let backend_impl = backend_impl_for(registry, &name)
                .unwrap_or_else(|_| DEFAULT_BACKEND_IMPL.to_string());
            let Ok(unit) = unit_name(&name, &role, &backend_impl) else {
                continue;
            };
            // SEC-R34: off-loop via the real async child-process wait
            // -- see `run_systemctl`'s own doc for why this can't be a
            // synchronous call on the shared runtime.
            run_systemctl(
                systemctl_program,
                systemctl_mode,
                &["stop", &unit],
                systemctl_timeout,
            )
            .await;

            let still_current = store
                .snapshot(&name)
                .and_then(|rt| rt.last_active.get(&role).copied())
                == Some(ts);
            if !still_current {
                continue;
            }
            // F015 v4: keep_hmac=true -- the on-disk key file is owned
            // by the systemd unit (RuntimeDirectoryPreserve keeps it
            // across a stop; ExecStartPre regenerates if missing), so
            // the router's cache stays valid across this reap.
            store.forget(&name, true, false);
        }
    }
}

/// Single pass over the registry, removing any alias whose
/// `expires_at` is in the past. ADR-0010: an alias with a MALFORMED
/// `expires_at` is preserved (not silently dropped) so the operator
/// can clean it up by hand -- only a successfully-parsed, past-due
/// entry is removed.
pub fn alias_reaper_tick(registry: &ProjectRegistry, now: DateTime<Utc>) {
    let Ok(rows) = registry.list() else { return };
    for row in rows {
        for alias in &row.aliases {
            if alias.name.is_empty() || alias.expires_at.is_empty() {
                continue;
            }
            let Ok(exp) = parse_flexible(&alias.expires_at) else {
                continue; // malformed -- ADR-0010, leave it for the operator.
            };
            if exp <= now {
                let _ = registry.expire_alias(&row.name, &alias.name);
                eprintln!(
                    "Alias {:?} for project {:?} expired and was removed.",
                    alias.name, row.name
                );
            }
        }
    }
}

/// Adopt already-running backend units after a router restart. Without
/// this, a router crash+restart would orphan every active backend --
/// the units stay up (systemd owns them) but the router has no
/// `last_active` entry, so the reaper never considers them for idle
/// timeout. Seeds `last_active` with `now` for every active unit
/// matching either template; the reaper's idle window starts fresh
/// from this seed (an actually-idle backend survives one extra `idle`
/// window after a restart, which is benign, matching Python's own
/// documented tradeoff).
pub async fn reconcile_on_startup(
    store: &RuntimeStore,
    now: SystemTime,
    systemctl_program: &str,
    systemctl_mode: SystemctlMode,
    systemctl_timeout: Duration,
) {
    let result = run_systemctl(
        systemctl_program,
        systemctl_mode,
        &[
            "list-units",
            "--type=service",
            "--state=active",
            "--no-legend",
            "--plain",
            "agent-mcp@*",
            "conexus@*",
        ],
        systemctl_timeout,
    )
    .await;

    for line in result.stdout.lines() {
        let Some(unit) = line.split_whitespace().next() else {
            continue;
        };
        let name = unit
            .strip_prefix("agent-mcp@")
            .or_else(|| unit.strip_prefix("conexus@"))
            .and_then(|rest| rest.strip_suffix(".service"));
        let Some(name) = name else { continue };
        store.with_runtime_mut(name, |rt| {
            rt.last_active.insert("backend".to_string(), now);
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    fn registry_with(dir: &Path, name: &str, backend_impl: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .register(name, "/ws/proj-a", backend_impl, now)
            .unwrap();
        registry
    }

    /// A disposable fake `systemctl` recording every invocation to a
    /// log file, optionally sleeping `delay_ms` before exiting `rc`
    /// for every call, or answering a canned `list-units` body.
    fn write_fake_systemctl(
        dir: &Path,
        rc: i32,
        delay_ms: u64,
        list_units_body: &str,
    ) -> (PathBuf, PathBuf) {
        let log = dir.join("calls.log");
        let script_path = dir.join("fake-systemctl.sh");
        let script = format!(
            r#"#!/bin/sh
echo "$@" >> "{log}"
for a in "$@"; do
  case "$a" in
    list-units) echo '{list_units_body}'; exit 0 ;;
  esac
done
sleep {delay}
exit {rc}
"#,
            log = log.display(),
            list_units_body = list_units_body,
            delay = delay_ms as f64 / 1000.0,
        );
        std::fs::write(&script_path, script).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&script_path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&script_path, perms).unwrap();
        }
        (script_path, log)
    }

    #[tokio::test]
    async fn reaper_tick_stops_and_forgets_a_genuinely_idle_backend() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let old_ts = SystemTime::now() - Duration::from_secs(1000);
        store.with_runtime_mut("proj-a", |rt| {
            rt.last_active.insert("backend".to_string(), old_ts);
        });
        let (program, log) = write_fake_systemctl(dir.path(), 0, 0, "");

        reaper_tick(
            &store,
            &registry,
            Duration::from_secs(500),
            SystemTime::now(),
            program.to_str().unwrap(),
            SystemctlMode::User,
            Duration::from_secs(5),
        )
        .await;

        assert!(
            store.snapshot("proj-a").is_none(),
            "an idle backend must be forgotten"
        );
        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(calls.contains("stop") && calls.contains("agent-mcp@proj-a.service"));
    }

    #[tokio::test]
    async fn reaper_tick_leaves_a_fresh_backend_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        store.with_runtime_mut("proj-a", |rt| {
            rt.last_active
                .insert("backend".to_string(), SystemTime::now());
        });
        let (program, log) = write_fake_systemctl(dir.path(), 0, 0, "");

        reaper_tick(
            &store,
            &registry,
            Duration::from_secs(500),
            SystemTime::now(),
            program.to_str().unwrap(),
            SystemctlMode::User,
            Duration::from_secs(5),
        )
        .await;

        assert!(
            store.snapshot("proj-a").is_some(),
            "a fresh backend must not be reaped"
        );
        assert!(
            !log.exists(),
            "a fresh backend must never invoke systemctl at all"
        );
    }

    #[tokio::test]
    async fn reaper_tick_does_not_forget_a_backend_reactivated_during_the_stop() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = std::sync::Arc::new(RuntimeStore::new());
        let old_ts = SystemTime::now() - Duration::from_secs(1000);
        store.with_runtime_mut("proj-a", |rt| {
            rt.last_active.insert("backend".to_string(), old_ts);
        });
        // `stop` sleeps 200ms -- long enough for a concurrent
        // reactivation to land mid-flight, a real (if scripted) race.
        let (program, _log) = write_fake_systemctl(dir.path(), 0, 200, "");

        let store2 = store.clone();
        let reaper_fut = reaper_tick(
            &store,
            &registry,
            Duration::from_secs(500),
            SystemTime::now(),
            program.to_str().unwrap(),
            SystemctlMode::User,
            Duration::from_secs(5),
        );
        let reactivate_fut = async {
            tokio::time::sleep(Duration::from_millis(50)).await;
            store2.with_runtime_mut("proj-a", |rt| {
                rt.last_active
                    .insert("backend".to_string(), SystemTime::now());
            });
        };
        tokio::join!(reaper_fut, reactivate_fut);

        assert!(
            store.snapshot("proj-a").is_some(),
            "a backend reactivated WHILE the stop was in flight must not be dropped from tracking"
        );
    }

    #[test]
    fn alias_reaper_tick_removes_an_expired_alias_and_preserves_a_live_one() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "python");
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .add_alias("proj-a", "expired-alias", None, Some(-1), now)
            .unwrap();
        registry
            .add_alias("proj-a", "live-alias", None, Some(30), now)
            .unwrap();

        alias_reaper_tick(&registry, now);

        let row = registry.get("proj-a").unwrap().unwrap();
        let names: Vec<&str> = row.aliases.iter().map(|a| a.name.as_str()).collect();
        assert_eq!(names, vec!["live-alias"]);
    }

    #[test]
    fn alias_reaper_tick_preserves_a_malformed_expires_at() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("projects.local.json");
        std::fs::write(
            &path,
            r#"{"proj-a": {"workspace": "/ws/proj-a", "aliases": [{"name": "bad-alias", "expires_at": "not-a-real-timestamp"}]}}"#,
        )
        .unwrap();
        let registry = ProjectRegistry::new(path);
        let now: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();

        alias_reaper_tick(&registry, now);

        let row = registry.get("proj-a").unwrap().unwrap();
        assert_eq!(
            row.aliases.len(),
            1,
            "a malformed expires_at must be preserved, not dropped"
        );
    }

    #[tokio::test]
    async fn reconcile_on_startup_adopts_both_python_and_rust_units() {
        let dir = tempfile::tempdir().unwrap();
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let list_units_body = "agent-mcp@proj-a.service loaded active running Agent-MCP\nconexus@proj-b.service loaded active running CoNexus";
        let (program, _log) = write_fake_systemctl(dir.path(), 0, 0, list_units_body);

        let now = SystemTime::now();
        reconcile_on_startup(
            &store,
            now,
            program.to_str().unwrap(),
            SystemctlMode::User,
            Duration::from_secs(5),
        )
        .await;

        assert_eq!(
            store.snapshot("proj-a").unwrap().last_active.get("backend"),
            Some(&now),
            "the agent-mcp@ (Python) unit must be adopted"
        );
        assert_eq!(
            store.snapshot("proj-b").unwrap().last_active.get("backend"),
            Some(&now),
            "the conexus@ (Rust) unit must ALSO be adopted -- the real fix for the found Python gap"
        );
        let _ = registry;
    }

    #[tokio::test]
    async fn reconcile_on_startup_ignores_unrelated_unit_lines() {
        let dir = tempfile::tempdir().unwrap();
        let store = RuntimeStore::new();
        let (program, _log) = write_fake_systemctl(
            dir.path(),
            0,
            0,
            "some-other-thing.service loaded active running Other",
        );

        reconcile_on_startup(
            &store,
            SystemTime::now(),
            program.to_str().unwrap(),
            SystemctlMode::User,
            Duration::from_secs(5),
        )
        .await;

        assert!(store.snapshot_all().is_empty());
    }
}
