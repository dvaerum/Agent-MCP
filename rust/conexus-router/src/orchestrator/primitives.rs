//! Backend lifecycle primitives -- port of `project_orchestrator.py`'s
//! "Backend lifecycle primitives" section (lines 379-590, Phase E2
//! PR 6b): socket/HMAC-key path resolution, unit-name resolution
//! (the Phase D1 `backend_impl` branch), and the `systemctl`
//! shell-out. Pure I/O boundaries -- no state-machine decisions live
//! here (that's PR 6c's `ensure()`).
//!
//! **`tokio::process::Command`, not `std::process::Command` +
//! `spawn_blocking`**: this binary is already `#[tokio::main]` with
//! `rt-multi-thread` (main.rs), so a genuinely async child-process
//! wait is more idiomatic here than the `spawn_blocking`-wrapped
//! `std::process::Command` shape Python's own `asyncio.to_thread(
//! subprocess.run, ...)` calls for -- no existing workspace precedent
//! for shelling out at all, so this is a fresh, deliberate choice, not
//! a fork resolution.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use crate::orchestrator::runtime::RuntimeStore;
use crate::project_registry::{ProjectRegistry, RegistryError, DEFAULT_BACKEND_IMPL};

/// `SOCK_DIR/<name>/<role>.sock` -- mkdir-as-side-effect, matching
/// Python's own `_sock_path`. `role` is always `"backend"` today
/// (kept role-generic to match Python's own forward-looking shape).
pub fn sock_path(sock_dir: &Path, name: &str, role: &str) -> std::io::Result<PathBuf> {
    let dir = sock_dir.join(name);
    std::fs::create_dir_all(&dir)?;
    Ok(dir.join(format!("{role}.sock")))
}

/// `SOCK_DIR/<name>/forwarding_hmac` -- same directory as the UDS
/// socket, same mkdir side effect. The router only ever READS this
/// path (F015 v4 -- the systemd unit's own `ExecStartPre` owns key
/// generation; a router-side write here would reopen the exact
/// restart-loop bug F015 v4 fixed).
pub fn forwarding_hmac_path(sock_dir: &Path, name: &str) -> std::io::Result<PathBuf> {
    let dir = sock_dir.join(name);
    std::fs::create_dir_all(&dir)?;
    Ok(dir.join("forwarding_hmac"))
}

/// Return `name`'s cached-or-disk-read forwarding HMAC key. Read-only
/// (F015 v4): never generates a key, never writes to disk. Cache hit
/// -> no I/O; cache miss -> read the file (missing file, or any other
/// read error, degrades to `Ok(None)` with a logged warning for the
/// latter -- matching Python's `FileNotFoundError` vs. bare `OSError`
/// split exactly); a successful read populates [`RuntimeStore`]'s
/// cache before returning.
pub fn ensure_forwarding_hmac_key(
    store: &RuntimeStore,
    sock_dir: &Path,
    name: &str,
) -> std::io::Result<Option<Vec<u8>>> {
    if let Some(cached) = store.snapshot(name).and_then(|rt| rt.forwarding_hmac_key) {
        return Ok(Some(cached));
    }
    let path = forwarding_hmac_path(sock_dir, name)?;
    match std::fs::read(&path) {
        Ok(bytes) if !bytes.is_empty() => {
            store.with_runtime_mut(name, |rt| rt.forwarding_hmac_key = Some(bytes.clone()));
            Ok(Some(bytes))
        }
        Ok(_) => Ok(None),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => {
            eprintln!("failed to read HMAC key for project {name:?}: {e}");
            Ok(None)
        }
    }
}

/// Resolve `name`'s registry `backend_impl`, defaulting to
/// [`DEFAULT_BACKEND_IMPL`] when the project is unknown -- a
/// concurrent delete/rename can race a reaper or stop call that's
/// already mid-flight (winding a project DOWN, not creating one), so
/// falling back preserves today's only behavior (the `agent-mcp@`
/// template) rather than propagating an error.
pub fn backend_impl_for(registry: &ProjectRegistry, name: &str) -> Result<String, RegistryError> {
    Ok(registry
        .get(name)?
        .map(|p| p.backend_impl)
        .unwrap_or_else(|| DEFAULT_BACKEND_IMPL.to_string()))
}

#[derive(Debug)]
pub enum UnitNameError {
    UnsupportedRole(String),
}

impl std::fmt::Display for UnitNameError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UnitNameError::UnsupportedRole(role) => write!(f, "unsupported role: {role:?}"),
        }
    }
}

impl std::error::Error for UnitNameError {}

/// Systemd unit name for `(name, role)`. `backend_impl` selects
/// between the Python (`agent-mcp@`) and Rust/CoNexus (`conexus@`)
/// unit templates -- literally `"rust"` selects `conexus@`, anything
/// else (including a malformed value) falls through to `agent-mcp@`,
/// matching Python's own un-validated `if backend_impl == "rust":
/// ... else: ...` exactly (validation against
/// `project_registry::VALID_BACKEND_IMPLS` is the REGISTRY's job, not
/// this function's).
///
/// Deliberately takes `backend_impl: &str` directly rather than
/// Python's `backend_impl: str | None = None`-resolves-internally
/// shape -- the caller decides whether to pass an already-fetched
/// project row's field (avoiding a second registry lock-and-read, the
/// exact optimization `_ensure`'s own Python call site makes) or a
/// fresh [`backend_impl_for`] lookup (the shape `stop()`/the reaper
/// need, with no project row in hand). Both call shapes compose from
/// this one pure function instead of an internal branch.
///
/// Per the operator's locked decision (Phase D1): both templates
/// share the same `RuntimeDirectory`/socket path (see [`sock_path`],
/// unaffected by `backend_impl`) -- a cutover is a same-path process
/// swap, not a socket migration.
pub fn unit_name(name: &str, role: &str, backend_impl: &str) -> Result<String, UnitNameError> {
    if role != "backend" {
        return Err(UnitNameError::UnsupportedRole(role.to_string()));
    }
    if backend_impl == "rust" {
        Ok(format!("conexus@{name}.service"))
    } else {
        Ok(format!("agent-mcp@{name}.service"))
    }
}

/// Whether to call `systemctl --user` (default, matches the
/// nixos-developer-system / home-manager deployment) or plain
/// `systemctl` (system mode -- an in-VM flake deployment where the
/// router runs as a root system service with no D-Bus session bus).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SystemctlMode {
    User,
    System,
}

impl SystemctlMode {
    /// Port of the `AGENT_MCP_SYSTEMCTL_MODE` env-var contract
    /// (case-insensitive, trimmed; anything other than exactly
    /// `"user"` means system mode) -- `get_env` is an explicit lookup,
    /// not a direct `std::env::var` read, matching the Phase D2
    /// RAG-clients / `project_registry.rs` convention.
    pub fn from_env(get_env: impl Fn(&str) -> Option<String>) -> Self {
        let raw = get_env("AGENT_MCP_SYSTEMCTL_MODE").unwrap_or_else(|| "user".to_string());
        if raw.trim().to_lowercase() == "user" {
            SystemctlMode::User
        } else {
            SystemctlMode::System
        }
    }
}

/// The outcome of one `systemctl` invocation -- port of the fields of
/// Python's `subprocess.CompletedProcess` that `_systemctl`'s callers
/// actually read.
#[derive(Debug, Clone)]
pub struct SystemctlResult {
    pub returncode: i32,
    pub stdout: String,
    pub stderr: String,
}

impl SystemctlResult {
    pub fn success(&self) -> bool {
        self.returncode == 0
    }
}

/// Run `<program> [--user] <args...>` with a wall-clock `timeout`,
/// generic over which binary to invoke -- production always uses
/// [`systemctl`]'s `"systemctl"`; tests point `program` at a real,
/// disposable script so the timeout-wrapping and argument-construction
/// logic below is proven against a REAL child process, matching
/// Python's own `test_sec_r7_boot_aware_restart.py` (which tests
/// `_systemctl` against the real `subprocess.run`, not a stub).
///
/// On timeout, synthesizes a `returncode: 124` result (mirroring
/// coreutils `timeout`'s exit code, same as Python) instead of
/// propagating an error -- every caller's existing "non-zero
/// returncode" branch handles it the same way a genuine systemctl
/// failure would, so no caller needs a separate timeout code path.
pub async fn run_systemctl(
    program: &str,
    mode: SystemctlMode,
    args: &[&str],
    timeout: Duration,
) -> SystemctlResult {
    let mut cmd = tokio::process::Command::new(program);
    if mode == SystemctlMode::User {
        cmd.arg("--user");
    }
    cmd.args(args);
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());
    cmd.stdin(Stdio::null());

    match tokio::time::timeout(timeout, cmd.output()).await {
        Ok(Ok(output)) => SystemctlResult {
            returncode: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        },
        Ok(Err(e)) => SystemctlResult {
            returncode: -1,
            stdout: String::new(),
            stderr: format!("failed to spawn {program}: {e}"),
        },
        Err(_) => {
            let verb = args.join(" ");
            let secs = timeout.as_secs_f64();
            eprintln!("{program} {verb} timed out after {secs:.0}s");
            SystemctlResult {
                returncode: 124,
                stdout: String::new(),
                stderr: format!("{program} {verb} timed out after {secs:.0}s"),
            }
        }
    }
}

/// `systemctl [--user] <args...>` -- the real production entry point,
/// thin wrapper over [`run_systemctl`] with `program` fixed to
/// `"systemctl"`.
pub async fn systemctl(mode: SystemctlMode, args: &[&str], timeout: Duration) -> SystemctlResult {
    run_systemctl("systemctl", mode, args, timeout).await
}

/// `systemctl [--user] is-active <unit>` -- `true` iff the returncode
/// is `0`.
pub async fn is_active(mode: SystemctlMode, unit: &str, timeout: Duration) -> bool {
    systemctl(mode, &["is-active", unit], timeout)
        .await
        .success()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project_registry::ProjectRegistry;

    #[test]
    fn sock_path_creates_the_directory_and_appends_the_role_suffix() {
        let dir = tempfile::tempdir().unwrap();
        let path = sock_path(dir.path(), "proj-a", "backend").unwrap();
        assert_eq!(path, dir.path().join("proj-a").join("backend.sock"));
        assert!(dir.path().join("proj-a").is_dir());
    }

    #[test]
    fn forwarding_hmac_path_creates_the_directory_and_uses_the_fixed_name() {
        let dir = tempfile::tempdir().unwrap();
        let path = forwarding_hmac_path(dir.path(), "proj-a").unwrap();
        assert_eq!(path, dir.path().join("proj-a").join("forwarding_hmac"));
    }

    #[test]
    fn ensure_forwarding_hmac_key_reads_from_disk_and_populates_the_cache() {
        let dir = tempfile::tempdir().unwrap();
        let store = RuntimeStore::new();
        let path = forwarding_hmac_path(dir.path(), "proj-a").unwrap();
        std::fs::write(&path, b"secretkey").unwrap();

        let key = ensure_forwarding_hmac_key(&store, dir.path(), "proj-a")
            .unwrap()
            .unwrap();
        assert_eq!(key, b"secretkey");
        assert_eq!(
            store.snapshot("proj-a").unwrap().forwarding_hmac_key,
            Some(b"secretkey".to_vec()),
            "a successful disk read must populate the runtime cache"
        );
    }

    #[test]
    fn ensure_forwarding_hmac_key_cache_hit_never_touches_disk() {
        let dir = tempfile::tempdir().unwrap();
        let store = RuntimeStore::new();
        store.with_runtime_mut("proj-a", |rt| {
            rt.forwarding_hmac_key = Some(b"cached".to_vec())
        });
        // No file written at all -- a disk-reading implementation
        // would return None here; the cache must win first.
        let key = ensure_forwarding_hmac_key(&store, dir.path(), "proj-a")
            .unwrap()
            .unwrap();
        assert_eq!(key, b"cached");
    }

    #[test]
    fn ensure_forwarding_hmac_key_missing_file_is_none() {
        let dir = tempfile::tempdir().unwrap();
        let store = RuntimeStore::new();
        assert_eq!(
            ensure_forwarding_hmac_key(&store, dir.path(), "proj-a").unwrap(),
            None
        );
    }

    #[test]
    fn ensure_forwarding_hmac_key_empty_file_is_none() {
        let dir = tempfile::tempdir().unwrap();
        let store = RuntimeStore::new();
        let path = forwarding_hmac_path(dir.path(), "proj-a").unwrap();
        std::fs::write(&path, b"").unwrap();
        assert_eq!(
            ensure_forwarding_hmac_key(&store, dir.path(), "proj-a").unwrap(),
            None
        );
    }

    #[test]
    fn backend_impl_for_defaults_to_python_for_an_unknown_project() {
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        assert_eq!(backend_impl_for(&registry, "nope").unwrap(), "python");
    }

    #[test]
    fn backend_impl_for_reads_a_registered_projects_flag() {
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let now: chrono::DateTime<chrono::Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        registry
            .register("proj-a", "/ws/proj-a", "rust", now)
            .unwrap();
        assert_eq!(backend_impl_for(&registry, "proj-a").unwrap(), "rust");
    }

    #[test]
    fn unit_name_selects_conexus_for_rust_and_agent_mcp_otherwise() {
        assert_eq!(
            unit_name("proj-a", "backend", "rust").unwrap(),
            "conexus@proj-a.service"
        );
        assert_eq!(
            unit_name("proj-a", "backend", "python").unwrap(),
            "agent-mcp@proj-a.service"
        );
        assert_eq!(
            unit_name("proj-a", "backend", "totally-bogus").unwrap(),
            "agent-mcp@proj-a.service",
            "matching Python exactly: only the literal string \"rust\" selects conexus@"
        );
    }

    #[test]
    fn unit_name_rejects_an_unsupported_role() {
        let err = unit_name("proj-a", "frontend", "python").unwrap_err();
        assert!(matches!(err, UnitNameError::UnsupportedRole(role) if role == "frontend"));
    }

    #[test]
    fn systemctl_mode_from_env_matrix() {
        let case = |v: Option<&str>| {
            SystemctlMode::from_env(|k| {
                if k == "AGENT_MCP_SYSTEMCTL_MODE" {
                    v.map(str::to_string)
                } else {
                    None
                }
            })
        };
        assert_eq!(
            case(None),
            SystemctlMode::User,
            "unset defaults to user mode"
        );
        assert_eq!(case(Some("user")), SystemctlMode::User);
        assert_eq!(case(Some("USER")), SystemctlMode::User);
        assert_eq!(case(Some("  user  ")), SystemctlMode::User);
        assert_eq!(case(Some("system")), SystemctlMode::System);
        assert_eq!(case(Some("System")), SystemctlMode::System);
        assert_eq!(case(Some("anything-else")), SystemctlMode::System);
    }

    fn write_fake_systemctl(dir: &Path, script: &str) -> PathBuf {
        let path = dir.join("fake-systemctl.sh");
        std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&path, perms).unwrap();
        }
        path
    }

    #[tokio::test]
    async fn run_systemctl_prepends_user_flag_only_in_user_mode() {
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_systemctl(dir.path(), "echo \"$@\"");

        let user_result = run_systemctl(
            script.to_str().unwrap(),
            SystemctlMode::User,
            &["is-active", "proj-a.service"],
            Duration::from_secs(5),
        )
        .await;
        assert_eq!(user_result.stdout.trim(), "--user is-active proj-a.service");

        let system_result = run_systemctl(
            script.to_str().unwrap(),
            SystemctlMode::System,
            &["is-active", "proj-a.service"],
            Duration::from_secs(5),
        )
        .await;
        assert_eq!(system_result.stdout.trim(), "is-active proj-a.service");
    }

    #[tokio::test]
    async fn run_systemctl_surfaces_a_real_nonzero_exit_and_stderr() {
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_systemctl(dir.path(), "echo boom >&2; exit 3");

        let result = run_systemctl(
            script.to_str().unwrap(),
            SystemctlMode::User,
            &["start", "proj-a.service"],
            Duration::from_secs(5),
        )
        .await;
        assert_eq!(result.returncode, 3);
        assert_eq!(result.stderr.trim(), "boom");
        assert!(!result.success());
    }

    #[tokio::test]
    async fn run_systemctl_times_out_a_real_stalled_process_with_rc_124() {
        let dir = tempfile::tempdir().unwrap();
        // Sleeps far longer than the timeout below -- a REAL stalled
        // child process, not a simulated timeout.
        let script = write_fake_systemctl(dir.path(), "sleep 5");

        let result = run_systemctl(
            script.to_str().unwrap(),
            SystemctlMode::User,
            &["start", "proj-a.service"],
            Duration::from_millis(100),
        )
        .await;
        assert_eq!(
            result.returncode, 124,
            "124 mirrors coreutils timeout's exit code"
        );
        assert!(result.stderr.contains("timed out"));
    }

    #[tokio::test]
    async fn is_active_reflects_a_real_zero_exit() {
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_systemctl(dir.path(), "exit 0");
        assert!(run_systemctl(
            script.to_str().unwrap(),
            SystemctlMode::User,
            &["is-active", "x"],
            Duration::from_secs(5)
        )
        .await
        .success());
    }
}
