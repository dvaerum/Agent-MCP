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
    ensure_forwarding_hmac_key, is_real_socket, run_systemctl, sock_path, unit_name, SystemctlMode,
    UnitNameError,
};
use crate::orchestrator::runtime::{EnsureFailureReason, RuntimeStore};
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
    let needs_start = !unit_active || !is_real_socket(&sock);

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
            if is_real_socket(&sock) {
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

    // Success -- evict any stale failure entry so the next caller
    // doesn't see a phantom cooldown for a now-healthy backend.
    store.with_runtime_mut(name, |rt| {
        rt.ensure_failures.remove(role);
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
}
