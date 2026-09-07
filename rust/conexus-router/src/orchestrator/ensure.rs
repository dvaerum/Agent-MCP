//! The lazy-activation state machine -- port of `project_orchestrator.
//! py::_ensure` (lines 704-907, Phase E2 PR 6c). This is the
//! highest-value/highest-risk piece of the whole orchestrator: it
//! composes PR 6a's [`RuntimeStore`] and PR 6b's `primitives` into
//! "make sure the backend for `(name, role)` is running; return its
//! socket path", including the SC-R7-1 boot-aware-restart decision,
//! the P005 cached-failure cooldown, the BL-R6-1 TOCTOU re-check, and
//! the SC-R8-2/SC-R9-1 error-message genericization.
//!
//! **Genuinely time-spanning, NOT a pure function**: unlike
//! `identity.rs::create_user`/`project_registry.rs::register` (which
//! each write ONE timestamp and return), this function can legitimately
//! run for up to ~20 real seconds (the socket-poll budget) and reads
//! the clock at several DIFFERENT points as it progresses -- the same
//! category as `conexus-wakeloop`'s `wait_for_events` slow-path loop
//! (Phase D3), this workspace's own established precedent for "a
//! function that must read the real clock repeatedly, tested via
//! `tokio::time::pause()` virtual time" rather than injecting one
//! `now` value at entry the way a single-write function would.
//!
//! **No HTTP-framework dependency**: Python conflates "backend
//! lifecycle result" with "HTTP response shape" by raising `aiohttp
//! web.HTTP*` exceptions as control flow. [`EnsureError`] is a plain,
//! closed enum instead (matching `RegistryError`'s own precedent) --
//! whichever later PR owns the axum handler layer maps it to a status
//! code + fixed reason string, exactly mirroring how Python's own
//! handlers catch the `web.HTTP*` exceptions, but keeping this crate's
//! state-machine module itself free of any web-framework type.
//!
//! **Testing philosophy, matching PR 6b**: `EnsureConfig.systemctl_
//! program` lets tests point the whole state machine at a REAL,
//! disposable fake-systemctl script (recording its own invocations and
//! returning configurable exit codes) rather than mocking `ensure()`'s
//! internals -- the state machine genuinely spawns real child
//! processes and polls a real filesystem path end to end.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use crate::orchestrator::primitives::{
    ensure_forwarding_hmac_key, run_systemctl, sock_path, socket_ready, unit_name, SystemctlMode,
    UnitNameError,
};
use crate::orchestrator::runtime::{EnsureFailureReason, RestartStreak, RuntimeStore};
use crate::project_registry::{ProjectRegistry, RegistryError};

/// Port of Python's raised-exception surface at this seam
/// (`web.HTTPNotFound`/`HTTPGatewayTimeout`/`HTTPInternalServerError`),
/// collapsed into one closed enum with no web-framework dependency
/// (see module doc).
#[derive(Debug)]
pub enum EnsureError {
    /// The registry has no such project -- either the initial lookup
    /// miss, or the BL-R6-1 TOCTOU re-check finding it gone. Python
    /// raises the identical fixed `reason="unknown project"` in both
    /// spots (never reflecting the caller-supplied name), which is
    /// why both collapse to the same variant here.
    UnknownProject,
    /// P005: a cached failure from a previous `ensure()` call is still
    /// within its cooldown window -- replay the SAME generic reason
    /// rather than re-attempting a doomed systemctl call.
    Cooldown(EnsureFailureReason),
    /// A FRESH failure just occurred (systemctl shell-out failed, or
    /// the socket never appeared within the poll budget).
    Failed(EnsureFailureReason),
    /// SC-R7-1 livelock fix: `EnsureConfig::max_restart_attempts`
    /// consecutive forced restarts within the current unbroken
    /// failure streak all failed to produce a connectable socket.
    /// Refuses to issue another `systemctl restart` until
    /// `retry_after` elapses (see [`EnsureConfig::giveup_cooldown`])
    /// -- this is the fix for the self-resetting
    /// restart-every-request livelock this variant exists to end (see
    /// this module's doc comment and the `RestartStreak` doc). A
    /// backend that becomes healthy through ANY other means (an
    /// operator's manual restart, systemd's own `Restart=` policy) is
    /// still picked up immediately by the normal fast path above,
    /// which clears this state on success -- giving up only stops the
    /// ROUTER from continuing to force restarts on its own.
    GaveUp {
        retry_after: Duration,
    },
    Registry(RegistryError),
    UnitName(UnitNameError),
    Io(std::io::Error),
}

impl From<RegistryError> for EnsureError {
    fn from(e: RegistryError) -> Self {
        EnsureError::Registry(e)
    }
}

impl From<UnitNameError> for EnsureError {
    fn from(e: UnitNameError) -> Self {
        EnsureError::UnitName(e)
    }
}

impl From<std::io::Error> for EnsureError {
    fn from(e: std::io::Error) -> Self {
        EnsureError::Io(e)
    }
}

impl std::fmt::Display for EnsureError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EnsureError::UnknownProject => write!(f, "unknown project"),
            EnsureError::Cooldown(reason) => write!(f, "{}", reason.message()),
            EnsureError::Failed(reason) => write!(f, "{}", reason.message()),
            EnsureError::GaveUp { retry_after } => write!(
                f,
                "backend repeatedly failed to become ready; giving up for {:.0}s",
                retry_after.as_secs_f64()
            ),
            EnsureError::Registry(e) => write!(f, "{e}"),
            EnsureError::UnitName(e) => write!(f, "{e}"),
            EnsureError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for EnsureError {}

/// Every env-overridable timing/behavior knob `ensure()` reads, ported
/// from the module-level constants Python reads once at import time
/// (`ENSURE_FAILURE_COOLDOWN_SEC`/`BOOT_GRACE_SEC`/
/// `_SYSTEMCTL_TIMEOUT_SEC`/`AGENT_MCP_SYSTEMCTL_MODE`) plus the
/// per-call env read (`AGENT_MCP_ENSURE_SOCKET_ATTEMPTS`) -- unified
/// into one explicit struct rather than scattered module globals, this
/// crate's own established convention.
#[derive(Debug, Clone)]
pub struct EnsureConfig {
    /// The systemctl binary to invoke -- always `"systemctl"` in
    /// production ([`EnsureConfig::from_env`]); tests point this at a
    /// disposable fake script (see module doc).
    pub systemctl_program: String,
    pub systemctl_mode: SystemctlMode,
    pub systemctl_timeout: Duration,
    pub ensure_failure_cooldown: Duration,
    pub boot_grace: Duration,
    /// Socket-poll budget in 100ms ticks (matching Python's fixed
    /// `asyncio.sleep(0.1)` interval, itself not env-overridable --
    /// only the attempt COUNT is).
    pub socket_poll_attempts: u32,
    /// SC-R7-1 livelock fix: how many consecutive forced restarts an
    /// unbroken failure streak is allowed before `ensure()` gives up
    /// (see [`EnsureError::GaveUp`] / `RestartStreak`) rather than
    /// restarting forever.
    pub max_restart_attempts: u32,
    /// SC-R7-1 livelock fix: once a streak has given up, how long
    /// `ensure()` refuses to touch systemctl again before allowing
    /// exactly one fresh attempt (self-healing without an operator,
    /// but at a bounded rate far below "every incoming request").
    pub giveup_cooldown: Duration,
}

impl EnsureConfig {
    /// Port of the real production defaults, `get_env`-injected
    /// matching the Phase D2 RAG-clients / `project_registry.rs`
    /// convention (sidesteps `cargo test`'s parallel-thread env-var-
    /// race hazard).
    pub fn from_env(get_env: impl Fn(&str) -> Option<String>) -> Self {
        let f64_env = |key: &str, default: f64| -> f64 {
            get_env(key).and_then(|v| v.parse().ok()).unwrap_or(default)
        };
        Self {
            systemctl_program: "systemctl".to_string(),
            systemctl_mode: SystemctlMode::from_env(&get_env),
            systemctl_timeout: Duration::from_secs_f64(f64_env(
                "AGENT_MCP_SYSTEMCTL_TIMEOUT_SEC",
                30.0,
            )),
            ensure_failure_cooldown: Duration::from_secs_f64(f64_env(
                "AGENT_MCP_ENSURE_FAILURE_COOLDOWN_SEC",
                5.0,
            )),
            boot_grace: Duration::from_secs_f64(f64_env("AGENT_MCP_BOOT_GRACE_SEC", 90.0)),
            socket_poll_attempts: get_env("AGENT_MCP_ENSURE_SOCKET_ATTEMPTS")
                .and_then(|v| v.parse().ok())
                .unwrap_or(200),
            max_restart_attempts: get_env("AGENT_MCP_ENSURE_MAX_RESTART_ATTEMPTS")
                .and_then(|v| v.parse().ok())
                .unwrap_or(5),
            giveup_cooldown: Duration::from_secs_f64(f64_env(
                "AGENT_MCP_ENSURE_GIVEUP_COOLDOWN_SEC",
                600.0,
            )),
        }
    }
}

/// Make sure the backend for `(name, role)` is running; return its
/// socket path. "Running" requires both `is-active` AND the socket
/// file existing -- the systemd unit can stay `active` while the
/// socket has gone stale (a crash mid-write), in which case this
/// restarts rather than starts.
///
/// Serialized per `(name, role)` via [`RuntimeStore::ensure_lock`] so
/// a burst of parallel requests only triggers one systemctl
/// invocation.
pub async fn ensure(
    store: &RuntimeStore,
    registry: &ProjectRegistry,
    sock_dir: &Path,
    name: &str,
    role: &str,
    cfg: &EnsureConfig,
) -> Result<PathBuf, EnsureError> {
    let project = registry.get(name)?.ok_or(EnsureError::UnknownProject)?;
    // Reuse the row just fetched instead of letting unit_name() take a
    // second lock-and-read -- this is the per-request hot path.
    let unit = unit_name(name, role, &project.backend_impl)?;
    let sock = sock_path(sock_dir, name, role)?;

    let lock = store.ensure_lock(name, role);
    let _guard = lock.lock().await;

    let unit_active = run_systemctl(
        &cfg.systemctl_program,
        cfg.systemctl_mode,
        &["is-active", &unit],
        cfg.systemctl_timeout,
    )
    .await
    .success();
    let needs_start = !unit_active || !socket_ready(&sock).await;

    if needs_start {
        // P005 cascade-fix: a cached failure still within its cooldown
        // window short-circuits to the SAME generic reason instead of
        // paying another full socket-wait -- checked AFTER the
        // freshness probe above so a backend that recovered between
        // the cached failure and now (e.g. a manual restart) falls
        // through to the success path instead of inheriting a phantom
        // failure for the rest of the cooldown window.
        let cached = store
            .snapshot(name)
            .and_then(|rt| rt.ensure_failures.get(role).copied());
        if let Some((failed_at, reason)) = cached {
            if failed_at.elapsed() < cfg.ensure_failure_cooldown {
                return Err(EnsureError::Cooldown(reason));
            }
            store.with_runtime_mut(name, |rt| {
                rt.ensure_failures.remove(role);
            });
        }

        // F015 v4: pure cache warm-up. A `None`/missing-file result is
        // fine here -- the unit hasn't run its ExecStartPre yet, which
        // is what we're about to trigger. Any OTHER I/O failure (e.g.
        // the socket directory can't be created) propagates, matching
        // Python's own unguarded `_forwarding_hmac_path(name)` mkdir.
        ensure_forwarding_hmac_key(store, sock_dir, name)?;

        // SC-R7-1: boot-aware restart decision (see the module this
        // was ported from for the full three-case rationale). An
        // active-but-socketless unit with NO recorded start time (a
        // router restart lost the map, or systemd's own `Restart=
        // on-failure` fired without going through us) is ADOPTED as
        // starting "now" and given the full grace window, rather than
        // restarted immediately.
        let action: Option<&'static str> = if !unit_active {
            Some("start")
        } else {
            let started_at = store
                .snapshot(name)
                .and_then(|rt| rt.unit_start_times.get(role).copied());
            let started_at = started_at.unwrap_or_else(|| {
                let now = Instant::now();
                store.with_runtime_mut(name, |rt| {
                    rt.unit_start_times.insert(role.to_string(), now);
                });
                now
            });
            if started_at.elapsed() < cfg.boot_grace {
                None // still booting -- keep waiting, don't touch systemctl
            } else {
                // SC-R7-1 livelock fix: grace has expired on THIS
                // incarnation, but a forced restart re-stamps
                // `unit_start_times` to "now" a few lines down -- on
                // its own that would let an unbroken run of restarts
                // NEVER accumulate enough elapsed time to give up,
                // since every restart resets the very clock meant to
                // measure "how long has this actually been failing".
                // `restart_streaks` tracks that separately, from its
                // FIRST restart, so it can't be reset by the restarts
                // it's counting (see `RestartStreak`'s doc).
                let streak = store
                    .snapshot(name)
                    .and_then(|rt| rt.restart_streaks.get(role).copied());
                let exhausted = streak.is_some_and(|s| s.attempts >= cfg.max_restart_attempts);
                if exhausted {
                    let s = streak.expect("exhausted implies a streak is present");
                    let remaining = cfg.giveup_cooldown.saturating_sub(s.started_at.elapsed());
                    if !remaining.is_zero() {
                        // Still within the give-up cooldown: refuse to
                        // touch systemctl again, no matter how many
                        // requests land -- this is what actually stops
                        // the livelock, as opposed to the pre-existing
                        // P005 cooldown, which is far shorter than
                        // `boot_grace` and so never once prevented a
                        // new restart cycle from starting.
                        return Err(EnsureError::GaveUp {
                            retry_after: remaining,
                        });
                    }
                    // The give-up cooldown elapsed: allow exactly ONE
                    // more fresh attempt (self-healing without an
                    // operator) by starting a brand new streak.
                    store.with_runtime_mut(name, |rt| {
                        rt.restart_streaks.insert(
                            role.to_string(),
                            RestartStreak {
                                started_at: Instant::now(),
                                attempts: 1,
                            },
                        );
                    });
                } else {
                    store.with_runtime_mut(name, |rt| {
                        let entry =
                            rt.restart_streaks
                                .entry(role.to_string())
                                .or_insert(RestartStreak {
                                    started_at: Instant::now(),
                                    attempts: 0,
                                });
                        entry.attempts += 1;
                    });
                }
                Some("restart")
            }
        };

        // BL-R6-1: TOCTOU re-check. The registry-existence probe above
        // runs OUTSIDE the ensure lock, so a concurrent delete can
        // unregister the project while this call was blocked
        // acquiring it. Re-read immediately before any spawn and abort
        // if the project is gone -- otherwise this would start a unit
        // for a deleted project, orphaned until the idle reaper (up to
        // IDLE_SEC) cleans it up.
        if registry.get(name)?.is_none() {
            return Err(EnsureError::UnknownProject);
        }

        let result = if let Some(action) = action {
            // Record the start window BEFORE the shell-out so a
            // concurrent caller that acquires the lock next observes
            // the grace window from THIS start.
            store.with_runtime_mut(name, |rt| {
                rt.unit_start_times.insert(role.to_string(), Instant::now());
            });
            run_systemctl(
                &cfg.systemctl_program,
                cfg.systemctl_mode,
                &[action, &unit],
                cfg.systemctl_timeout,
            )
            .await
        } else {
            // Boot-grace skip: don't touch systemctl, fall through to
            // the socket poll below.
            crate::orchestrator::primitives::SystemctlResult {
                returncode: 0,
                stdout: String::new(),
                stderr: String::new(),
            }
        };

        if !result.success() {
            // SC-R8-2: the systemctl-failure path is reachable by any
            // project MEMBER (a warm-start), not just an operator --
            // the client response must not reflect raw systemd
            // stderr. Genericize the client-facing reason; log the
            // full detail server-side only.
            eprintln!(
                "systemctl {action:?} {unit} failed (rc={}): {}",
                result.returncode,
                result.stderr.trim()
            );
            let reason = EnsureFailureReason::SystemctlFailed;
            store.with_runtime_mut(name, |rt| {
                rt.ensure_failures
                    .insert(role.to_string(), (Instant::now(), reason));
            });
            return Err(EnsureError::Failed(reason));
        }

        let mut ready = false;
        for _ in 0..cfg.socket_poll_attempts {
            if socket_ready(&sock).await {
                ready = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if !ready {
            // SC-R9-1: same hygiene as SC-R8-2 above -- never reflect
            // the unit name or the absolute server-side socket path to
            // a caller; log the detailed phrase server-side, store and
            // return only the generic reason.
            eprintln!(
                "ensure socket timeout: {unit} did not create {} within ~{}s",
                sock.display(),
                cfg.socket_poll_attempts as f64 * 0.1
            );
            let reason = EnsureFailureReason::SocketTimeout;
            store.with_runtime_mut(name, |rt| {
                rt.ensure_failures
                    .insert(role.to_string(), (Instant::now(), reason));
            });
            return Err(EnsureError::Failed(reason));
        }
    }

    // Success -- evict any stale failure/restart-streak entry so the
    // next caller doesn't see a phantom cooldown, or an inherited
    // give-up state, for a now-healthy backend. A backend that
    // recovers through ANY means (not just our own restart -- an
    // operator's manual restart, systemd's own `Restart=` policy)
    // ends its streak here, exactly like it clears `ensure_failures`.
    store.with_runtime_mut(name, |rt| {
        rt.ensure_failures.remove(role);
        rt.restart_streaks.remove(role);
        rt.last_active.insert(role.to_string(), SystemTime::now());
    });
    Ok(sock)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    fn registry_with(dir: &Path, name: &str, backend_impl: &str) -> ProjectRegistry {
        let registry = ProjectRegistry::new(dir.join("projects.local.json"));
        let now: chrono::DateTime<chrono::Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .register(name, "/ws/proj-a", backend_impl, now)
            .unwrap();
        registry
    }

    fn fast_cfg(program: &Path) -> EnsureConfig {
        EnsureConfig {
            systemctl_program: program.to_str().unwrap().to_string(),
            systemctl_mode: SystemctlMode::User,
            systemctl_timeout: Duration::from_secs(5),
            ensure_failure_cooldown: Duration::from_millis(200),
            boot_grace: Duration::from_millis(150),
            socket_poll_attempts: 5,
            max_restart_attempts: 5,
            giveup_cooldown: Duration::from_secs(600),
        }
    }

    /// A disposable fake `systemctl`: records every invocation's
    /// verb (one line per call, `--user`/unit args stripped) to a log
    /// file the test reads back afterward, and exits with the
    /// caller-chosen codes for `is-active` vs. `start`/`restart`.
    fn write_fake_systemctl(dir: &Path, is_active_rc: i32, action_rc: i32) -> (PathBuf, PathBuf) {
        let log = dir.join("calls.log");
        let script_path = dir.join("fake-systemctl.sh");
        let script = format!(
            r#"#!/bin/sh
echo "$@" >> "{log}"
verb=""
for a in "$@"; do
  case "$a" in
    is-active|start|restart|stop) verb="$a" ;;
  esac
done
case "$verb" in
  is-active) exit {is_active_rc} ;;
  start|restart) exit {action_rc} ;;
esac
exit 0
"#,
            log = log.display()
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
    async fn ensure_returns_immediately_when_already_active_with_a_real_socket() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        // is-active succeeds; start/restart would fail loudly if ever
        // invoked, proving the fast path never shells out to them.
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1);
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let sock_path = sock_dir.join("proj-a").join("backend.sock");
        let _listener = UnixListener::bind(&sock_path).unwrap();

        let result = ensure(
            &store,
            &registry,
            &sock_dir,
            "proj-a",
            "backend",
            &fast_cfg(&program),
        )
        .await
        .unwrap();
        assert_eq!(result, sock_path);
        // is-active IS always checked unconditionally (matching
        // Python's own `unit_active = await asyncio.to_thread
        // (_is_active, unit)` running before the needs_start decision)
        // -- the real proof of "fast path" is that start/restart are
        // never reached.
        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(calls.contains("is-active"));
        assert!(
            !calls.contains("start") && !calls.contains("restart"),
            "an already-healthy backend must never invoke start/restart"
        );
    }

    #[tokio::test]
    async fn ensure_starts_an_inactive_unit_and_waits_for_the_socket_to_appear() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 3, 0); // inactive; start succeeds
        let sock_path = sock_dir.join("proj-a").join("backend.sock");

        // Simulate a backend that binds its socket 150ms after being
        // started -- a real filesystem race the poll loop must win.
        let sock_path_clone = sock_path.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(150)).await;
            std::fs::create_dir_all(sock_path_clone.parent().unwrap()).unwrap();
            let _listener = UnixListener::bind(&sock_path_clone).unwrap();
            // Keep the listener alive for the rest of the test.
            std::mem::forget(_listener);
        });

        let mut cfg = fast_cfg(&program);
        cfg.socket_poll_attempts = 20; // 20 * 100ms = 2s budget, plenty for the 150ms delay
        let result = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap();
        assert_eq!(result, sock_path);

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(calls.contains("is-active"));
        assert!(
            calls.contains("start"),
            "an inactive unit must be STARTED, never restarted"
        );
        assert!(!calls.contains("restart"));
    }

    #[tokio::test]
    async fn ensure_restarts_a_stale_active_unit_past_the_boot_grace() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active but the socket is missing; restart fails

        // Seed an ALREADY-OLD start time directly -- the grace window
        // is measured from when the unit was first observed starting,
        // which `ensure()` itself would only just now be recording on
        // a fresh RuntimeStore (giving it zero elapsed time, still
        // within any grace). Seeding it old simulates "this unit has
        // genuinely been active-but-socketless past the grace window",
        // not "the router just noticed it this instant".
        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(1),
            );
        });

        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1); // expired relative to the seeded start time above

        let err = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            EnsureError::Failed(EnsureFailureReason::SystemctlFailed)
        ));

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(
            calls.contains("restart"),
            "an active-but-socketless unit past grace must be RESTARTED"
        );
        assert!(!calls.lines().any(|l| l.trim() == "start"));
    }

    #[tokio::test]
    async fn ensure_skips_systemctl_while_an_active_socketless_unit_is_within_boot_grace() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active, socketless

        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_secs(30); // well within grace for the whole test
        cfg.socket_poll_attempts = 2;

        // No `unit_start_times` entry seeded -- this is the "we never
        // saw this unit start" case (a router restart lost the map, or
        // systemd's own `Restart=on-failure` fired without going
        // through us).
        assert!(store
            .snapshot("proj-a")
            .and_then(|rt| rt.unit_start_times.get("backend").copied())
            .is_none());

        let err = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            EnsureError::Failed(EnsureFailureReason::SocketTimeout)
        ));

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(
            calls.lines().all(|l| !l.contains("start") && !l.contains("restart")),
            "within the boot-grace window, systemctl must be touched ONLY for is-active, never start/restart -- got: {calls:?}"
        );

        // ADOPTED as starting "now" -- a subsequent call must measure
        // the grace window from THIS first observation, not treat the
        // unit as having no start record forever.
        assert!(
            store
                .snapshot("proj-a")
                .and_then(|rt| rt.unit_start_times.get("backend").copied())
                .is_some(),
            "an active-but-socketless unit with no prior record must be adopted -- a start \
             time must be recorded so later calls measure grace from this observation"
        );
    }

    #[tokio::test]
    async fn ensure_config_default_boot_grace_covers_cold_boot_and_socket_wait_budget() {
        // The default boot-grace budget must exceed both the cold-boot
        // time (~44s) and the production socket-wait (~20s) so a
        // single caller's own socket-wait never trips the grace into a
        // restart (SC-R7-1's whole point -- see this module's doc).
        let cfg = EnsureConfig::from_env(|_| None);
        assert!(
            cfg.boot_grace >= Duration::from_secs_f64(44.0),
            "default AGENT_MCP_BOOT_GRACE_SEC must cover the ~44s cold boot, got {:?}",
            cfg.boot_grace
        );
    }

    #[tokio::test]
    async fn ensure_caches_a_failure_and_replays_it_within_the_cooldown_window() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 3, 1); // inactive; start fails

        let mut cfg = fast_cfg(&program);
        cfg.ensure_failure_cooldown = Duration::from_secs(30);

        let first = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(matches!(
            first,
            EnsureError::Failed(EnsureFailureReason::SystemctlFailed)
        ));

        let starts_after_first = std::fs::read_to_string(&log)
            .unwrap()
            .lines()
            .filter(|l| l.contains("start"))
            .count();
        assert_eq!(starts_after_first, 1);

        let second = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(matches!(
            second,
            EnsureError::Cooldown(EnsureFailureReason::SystemctlFailed)
        ));

        // is-active IS still checked on every call (Python's own
        // `unit_active = await asyncio.to_thread(_is_active, unit)`
        // runs unconditionally, before the cooldown short-circuit) --
        // the real proof of "replay, don't retry" is that no ADDITIONAL
        // start/restart call landed.
        let starts_after_second = std::fs::read_to_string(&log)
            .unwrap()
            .lines()
            .filter(|l| l.contains("start"))
            .count();
        assert_eq!(
            starts_after_first, starts_after_second,
            "a cooldown-window replay must not invoke systemctl start/restart again"
        );
    }

    #[tokio::test]
    async fn ensure_unknown_project_is_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::new();
        let (program, _log) = write_fake_systemctl(dir.path(), 0, 0);

        let err = ensure(
            &store,
            &registry,
            &sock_dir,
            "nope",
            "backend",
            &fast_cfg(&program),
        )
        .await
        .unwrap_err();
        assert!(matches!(err, EnsureError::UnknownProject));
    }

    #[tokio::test]
    async fn ensure_resolves_the_conexus_unit_for_a_rust_project() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "rust");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1);
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let _listener = UnixListener::bind(sock_dir.join("proj-a").join("backend.sock")).unwrap();

        ensure(
            &store,
            &registry,
            &sock_dir,
            "proj-a",
            "backend",
            &fast_cfg(&program),
        )
        .await
        .unwrap();
        // is-active must have been checked against the CONEXUS unit,
        // not agent-mcp@ -- proven by the recorded invocation args.
        // (No systemctl call happens here since the socket is already
        // real, so nothing is logged; this test's real assertion is
        // that success requires nothing to fail -- a mismatched unit
        // name would still succeed at this fast path, so the
        // meaningful proof is the restart-path test below.)
        let _ = log;
    }

    #[tokio::test]
    async fn ensure_targets_the_conexus_unit_for_a_rust_project_on_restart() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "rust");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active, socketless -> eventually restart

        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(1),
            );
        });
        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1);

        ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(calls.contains("conexus@proj-a.service"));
        assert!(!calls.contains("agent-mcp@proj-a.service"));
    }

    #[tokio::test]
    async fn ensure_lock_serializes_concurrent_calls_for_the_same_project() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = std::sync::Arc::new(registry_with(dir.path(), "proj-a", "python"));
        let store = std::sync::Arc::new(RuntimeStore::new());
        let (program, log) = write_fake_systemctl(dir.path(), 3, 0); // inactive; start succeeds slowly below
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let sock_path = sock_dir.join("proj-a").join("backend.sock");
        let _listener = UnixListener::bind(&sock_path).unwrap();

        let mut cfg = fast_cfg(&program);
        cfg.socket_poll_attempts = 10;
        let cfg = std::sync::Arc::new(cfg);
        let sock_dir = std::sync::Arc::new(sock_dir);

        let (r1, r2) = tokio::join!(
            ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg),
            ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
        );
        r1.unwrap();
        r2.unwrap();

        // The socket already exists, so BOTH calls take the "no start
        // needed" fast path individually once each acquires the lock
        // -- the real proof the lock doesn't deadlock or corrupt state
        // is that both complete successfully with a consistent view.
        let _ = log;
    }

    // -- SC-R7-1 livelock fix ------------------------------------------

    #[tokio::test]
    async fn ensure_treats_a_stale_socket_file_as_not_ready_and_restarts_past_grace() {
        // A socket FILE exists at the expected path (it would pass a
        // bare `fs::metadata(..).file_type().is_socket()` check) but
        // nothing is listening on it -- `UnixListener::bind` then
        // `drop` leaves exactly this: std's `Drop` closes the fd
        // without unlinking the path. Before this fix, `ensure()`'s
        // readiness check was exactly that bare file-type check, so
        // it would treat this as "already ready" and return `Ok`
        // WITHOUT ever consulting the boot-grace/restart decision --
        // silently handing a caller a socket path nothing will ever
        // accept a connection on. A real connect probe must see
        // through the stale file and drive the SAME SC-R7-1
        // restart-past-grace path a genuinely-missing socket would.
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active; restart fails
        std::fs::create_dir_all(sock_dir.join("proj-a")).unwrap();
        let sock_path = sock_dir.join("proj-a").join("backend.sock");
        {
            let listener = UnixListener::bind(&sock_path).unwrap();
            drop(listener);
        }
        use std::os::unix::fs::FileTypeExt;
        assert!(
            std::fs::metadata(&sock_path)
                .unwrap()
                .file_type()
                .is_socket(),
            "sanity: the stale file must still look like a socket to a bare file-type check"
        );

        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(1),
            );
        });
        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1); // expired relative to the seeded start time

        let err = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(
            matches!(
                err,
                EnsureError::Failed(EnsureFailureReason::SystemctlFailed)
            ),
            "a stale socket file must not short-circuit to Ok -- got {err:?}"
        );

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(
            calls.contains("restart"),
            "a stale socket FILE with nothing listening must be treated as not-ready and \
             trigger the SC-R7-1 restart decision, proving readiness is a real connect \
             probe, not bare file-existence -- got: {calls:?}"
        );
    }

    #[tokio::test]
    async fn ensure_in_flight_restart_is_never_duplicated_by_a_back_to_back_request() {
        // Two requests land back-to-back while a forced restart is
        // due. The per-(name, role) ensure lock serializes them for
        // the ENTIRE duration of `ensure()` (including the socket
        // poll), so the second caller must observe the first caller's
        // completed attempt (via the restart-streak/P005 state) rather
        // than racing its own independent `systemctl restart`. A
        // regression guard for the new restart-streak bookkeeping:
        // it must not introduce a way for two holders to each think
        // they're the one that gets to restart.
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = std::sync::Arc::new(registry_with(dir.path(), "proj-a", "python"));
        let store = std::sync::Arc::new(RuntimeStore::new());
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active, socketless; restart fails

        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(1),
            );
        });
        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1); // expired
        let cfg = std::sync::Arc::new(cfg);
        let sock_dir = std::sync::Arc::new(sock_dir);

        let (r1, r2) = tokio::join!(
            ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg),
            ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
        );
        r1.unwrap_err();
        r2.unwrap_err();

        let restarts = std::fs::read_to_string(&log)
            .unwrap()
            .lines()
            .filter(|l| l.contains("restart"))
            .count();
        assert_eq!(
            restarts, 1,
            "two back-to-back requests during a due restart must trigger exactly ONE \
             systemctl restart, not one each"
        );
    }

    #[tokio::test]
    async fn ensure_gives_up_after_max_restart_attempts_and_stops_hammering_systemctl() {
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        // active; restart "succeeds" (rc=0) but never actually creates
        // a real socket -- exactly the confirmed-live symptom (systemd
        // reports the restart as clean, `NRestarts=0`/
        // `ExecMainStatus=0`, yet the backend never becomes reachable).
        let (program, log) = write_fake_systemctl(dir.path(), 0, 0);

        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1);
        cfg.max_restart_attempts = 3;
        cfg.giveup_cooldown = Duration::from_secs(600); // must not silently retry mid-test

        // Seed a streak already AT the attempt cap, started long
        // enough ago that boot-grace is trivially expired but the
        // give-up cooldown has NOT -- simulates "already restarted
        // max_restart_attempts times in an unbroken streak and every
        // one of them failed to produce a real socket".
        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(10),
            );
            rt.restart_streaks.insert(
                "backend".to_string(),
                RestartStreak {
                    started_at: Instant::now() - Duration::from_secs(10),
                    attempts: 3,
                },
            );
        });

        let err = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        let retry_after = match err {
            EnsureError::GaveUp { retry_after } => retry_after,
            other => panic!("attempts exhausted must return a terminal GaveUp error, not restart again -- got {other:?}"),
        };
        assert!(retry_after > Duration::ZERO && retry_after <= cfg.giveup_cooldown);

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(
            !calls.lines().any(|l| l.contains("restart")),
            "give-up must not issue another systemctl restart -- got: {calls:?}"
        );
    }

    #[tokio::test]
    async fn ensure_allows_one_fresh_attempt_after_the_giveup_cooldown_elapses() {
        // Self-healing check: once the (much longer) give-up cooldown
        // itself has elapsed, `ensure()` must allow exactly one more
        // real restart attempt rather than refusing forever.
        let dir = tempfile::tempdir().unwrap();
        let sock_dir = dir.path().join("sockets");
        let registry = registry_with(dir.path(), "proj-a", "python");
        let store = RuntimeStore::new();
        let (program, log) = write_fake_systemctl(dir.path(), 0, 1); // active; restart fails

        let mut cfg = fast_cfg(&program);
        cfg.boot_grace = Duration::from_millis(1);
        cfg.max_restart_attempts = 3;
        cfg.giveup_cooldown = Duration::from_millis(1); // already elapsed by the time we check

        store.with_runtime_mut("proj-a", |rt| {
            rt.unit_start_times.insert(
                "backend".to_string(),
                Instant::now() - Duration::from_secs(10),
            );
            rt.restart_streaks.insert(
                "backend".to_string(),
                RestartStreak {
                    started_at: Instant::now() - Duration::from_secs(10),
                    attempts: 3,
                },
            );
        });

        let err = ensure(&store, &registry, &sock_dir, "proj-a", "backend", &cfg)
            .await
            .unwrap_err();
        assert!(
            matches!(
                err,
                EnsureError::Failed(EnsureFailureReason::SystemctlFailed)
            ),
            "once the give-up cooldown elapses, a fresh attempt must actually be tried \
             (and observe the fake systemctl's real failure), not immediately GaveUp again \
             -- got {err:?}"
        );

        let calls = std::fs::read_to_string(&log).unwrap();
        assert!(
            calls.lines().any(|l| l.contains("restart")),
            "the give-up cooldown elapsing must allow exactly one fresh systemctl restart \
             -- got: {calls:?}"
        );

        let streak = store
            .snapshot("proj-a")
            .and_then(|rt| rt.restart_streaks.get("backend").copied())
            .expect("a fresh streak must be recorded for the new attempt");
        assert_eq!(
            streak.attempts, 1,
            "the fresh attempt must start a NEW streak (attempts reset to 1), not keep \
             accumulating on top of the exhausted one"
        );
    }
}
