//! Synchronous decision functions for `rename_project_handler` --
//! confirmed the largest handler in `admin_api.py` (~470 LOC). Port
//! of its decision logic, split across its own 2 real yield points
//! (a body-read, an in-lock `systemctl stop` await) exactly like
//! `project_gate.rs`/`project_teardown.rs`'s own precedent: build the
//! synchronous DB/registry-side decision now, defer only the async
//! fusion wrapper (PR 23). Phase E2 PR 19, `conexus-router-rename-project`.
//!
//! Three functions, matching the 3 real phases of the handler:
//! [`rename_precheck`] (everything up to lock acquisition -- the
//! entry gate, name/grace_days validation, existence/collision/
//! active-conns checks -- spanning both the pre-body-read and post-
//! body-read halves, since neither carries an async yield point of
//! its own once the body is already parsed), [`rename_toctou_recheck`]
//! (the in-lock re-checks BEFORE the deferred `systemctl stop`), and
//! [`finish_rename_project`] (everything AFTER that await resolves:
//! `forget`, workspace move, token-file renames, the atomic registry
//! rename with its full error-mapping ladder, membership rekey,
//! runtime-dir purge).
#![allow(dead_code)]

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use rusqlite::Connection;

use crate::lifecycle::{self, LifecycleError};
use crate::mcp_handler::HandlerResponse;
use crate::orchestrator::runtime::RuntimeStore;
use crate::project_gate::{deny_cross_tenant_project_read, CrossTenantOutcome, GateError};
use crate::project_registry::{ProjectRegistry, ProjectRow, RegistryError};
use crate::project_teardown::active_connections;

const MAX_GRACE_DAYS: i64 = 3650;

fn invalid_grace_days(raw: &serde_json::Value) -> HandlerResponse {
    lifecycle::error_envelope(
        LifecycleError::InvalidName,
        &format!("grace_days must be an integer, got {raw:?}"),
        None,
    )
}

/// Port of the `int(grace_days_raw)` conversion, including PF-R18-1's
/// fix (`int(float('inf'))` raises `OverflowError` in Python, not
/// `ValueError`/`TypeError` -- an out-of-range/non-finite JSON number
/// must 400 here too, before any destructive step runs).
fn parse_grace_days(raw: Option<&serde_json::Value>) -> Result<i64, HandlerResponse> {
    let Some(value) = raw else {
        return Ok(30); // body.get("grace_days", 30)
    };
    match value {
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i)
            } else if let Some(f) = n.as_f64() {
                if f.is_finite() {
                    Ok(f as i64)
                } else {
                    Err(invalid_grace_days(value))
                }
            } else {
                Err(invalid_grace_days(value))
            }
        }
        serde_json::Value::Bool(b) => Ok(i64::from(*b)),
        serde_json::Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| invalid_grace_days(value)),
        _ => Err(invalid_grace_days(value)),
    }
}

#[derive(Debug)]
pub struct RenamePrecheckOk {
    pub old_name: String,
    pub new_name: String,
    pub grace_days: i64,
}

#[derive(Debug)]
pub enum RenamePrecheck {
    Proceed(RenamePrecheckOk),
    Rejected(HandlerResponse),
}

/// Port of `rename_project_handler`'s steps 1-14 (research report
/// section C): the entry gate, then every pure validation that must
/// happen BEFORE the destructive lock -- name/grace_days shape,
/// existence, alias collision (with the R1-F1 escape hatch), and the
/// `active_conns` guard.
#[allow(clippy::too_many_arguments)]
pub fn rename_precheck(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    old_name: &str,
    raw_new_name: Option<&serde_json::Value>,
    raw_grace_days: Option<&serde_json::Value>,
    now: DateTime<Utc>,
) -> Result<RenamePrecheck, GateError> {
    match deny_cross_tenant_project_read(
        conn,
        registry,
        is_sysadmin,
        caller_user_id,
        old_name,
        Some("operator"),
    )? {
        CrossTenantOutcome::Admit => {}
        CrossTenantOutcome::NotFound => {
            return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
                LifecycleError::NotFound,
                &format!("unknown project: {old_name:?}"),
                None,
            )));
        }
        CrossTenantOutcome::Forbidden { role, min_role } => {
            return Ok(RenamePrecheck::Rejected(forbidden_response(
                old_name, &role, &min_role,
            )));
        }
    }

    if let Some(resp) = lifecycle::reject_non_str_name(raw_new_name) {
        return Ok(RenamePrecheck::Rejected(resp));
    }
    let new_name = raw_new_name
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    let grace_days = match parse_grace_days(raw_grace_days) {
        Ok(v) => v,
        Err(resp) => return Ok(RenamePrecheck::Rejected(resp)),
    };
    if !(0..=MAX_GRACE_DAYS).contains(&grace_days) {
        return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::InvalidName,
            "grace_days must be between 0 and 3650",
            None,
        )));
    }
    if !lifecycle::SLUG_RE.is_match(old_name) {
        return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::InvalidName,
            &format!("old name {old_name:?} is not a valid slug"),
            None,
        )));
    }

    let existing: HashSet<String> = registry.list()?.into_iter().map(|p| p.name).collect();
    if let Some(msg) = lifecycle::validate_name(&new_name, &existing) {
        if msg.contains("already registered") {
            return Ok(RenamePrecheck::Rejected(name_collision_response(
                conn,
                registry,
                is_sysadmin,
                caller_user_id,
                &new_name,
                &msg,
            )?));
        }
        return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::InvalidName,
            &msg,
            None,
        )));
    }
    if old_name == new_name {
        return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::InvalidName,
            "old and new names are identical",
            None,
        )));
    }
    if registry.get(old_name)?.is_none() {
        return Ok(RenamePrecheck::Rejected(lifecycle::error_envelope(
            LifecycleError::NotRegistered,
            &format!("unknown project: {old_name:?}"),
            None,
        )));
    }
    if registry.resolve_alias(&new_name, now)?.is_some() {
        return Ok(RenamePrecheck::Rejected(alias_collision_response(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            &new_name,
            now,
        )?));
    }
    let conns = active_connections(store, old_name);
    if conns > 0 {
        return Ok(RenamePrecheck::Rejected(active_sessions_response(
            old_name, conns,
        )));
    }

    Ok(RenamePrecheck::Proceed(RenamePrecheckOk {
        old_name: old_name.to_string(),
        new_name,
        grace_days,
    }))
}

#[derive(Debug)]
pub struct RenameToctouOk {
    pub old_row: ProjectRow,
}

#[derive(Debug)]
pub enum RenameToctou {
    Proceed(RenameToctouOk),
    Rejected(HandlerResponse),
}

/// Port of the in-lock TOCTOU re-checks (PF-R36-1): two concurrent
/// renames of the SAME project serialise on `old_name`'s ensure-lock,
/// so the LOSING racer must see a clean re-check here rather than
/// crash into `registry.rename`'s own atomic guard.
#[allow(clippy::too_many_arguments)]
pub fn rename_toctou_recheck(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    old_name: &str,
    new_name: &str,
    now: DateTime<Utc>,
) -> Result<RenameToctou, GateError> {
    let Some(old_row) = registry.get(old_name)? else {
        return Ok(RenameToctou::Rejected(lifecycle::error_envelope(
            LifecycleError::NotRegistered,
            &format!("unknown project: {old_name:?}"),
            None,
        )));
    };
    if registry.resolve_alias(new_name, now)?.is_some() {
        return Ok(RenameToctou::Rejected(alias_collision_response(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            new_name,
            now,
        )?));
    }
    let conns = active_connections(store, old_name);
    if conns > 0 {
        return Ok(RenameToctou::Rejected(active_sessions_response(
            old_name, conns,
        )));
    }
    Ok(RenameToctou::Proceed(RenameToctouOk { old_row }))
}

#[derive(Debug)]
pub enum RenameOutcome {
    Renamed {
        from: String,
        to: String,
        grace_days: i64,
        alias_expires_at: String,
    },
    Rejected(HandlerResponse),
}

/// Port of everything AFTER the deferred `systemctl stop` await
/// resolves: `forget`, the workspace directory move, best-effort
/// token-file renames, the atomic `registry.rename` (with PF-R37-1's
/// full error-mapping ladder + a workspace-move rollback on failure),
/// a best-effort `project_membership` rekey (AZ-R13-1), and a
/// best-effort runtime-dir purge (BL-R35-1 parity with delete).
#[allow(clippy::too_many_arguments)]
pub fn finish_rename_project(
    conn: &Connection,
    registry: &ProjectRegistry,
    store: &RuntimeStore,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    old_name: &str,
    new_name: &str,
    grace_days: i64,
    old_row: &ProjectRow,
    sock_dir: &Path,
    token_dir: Option<&Path>,
    now: DateTime<Utc>,
) -> Result<RenameOutcome, GateError> {
    store.forget(old_name, false, true);

    let old_workspace = PathBuf::from(&old_row.workspace);
    let mut new_workspace: Option<PathBuf> = None;
    let workspace_is_conventional = old_workspace
        .file_name()
        .map(|n| n.to_string_lossy() == old_name)
        .unwrap_or(false);
    if workspace_is_conventional && old_workspace.exists() {
        let candidate = old_workspace.with_file_name(new_name);
        if let Err(e) = std::fs::rename(&old_workspace, &candidate) {
            return Ok(RenameOutcome::Rejected(lifecycle::error_envelope(
                LifecycleError::Internal,
                &format!("could not rename workspace dir: {e}"),
                None,
            )));
        }
        new_workspace = Some(candidate);
    }

    rename_token_files(token_dir, old_name, new_name);

    if let Err(e) = registry.rename(old_name, new_name, grace_days, now) {
        // Roll back the workspace move before reporting -- a
        // half-renamed project (registry on the old name, disk on the
        // new) must never be the observable end state of a failure.
        if let Some(nw) = &new_workspace {
            if nw.exists() {
                let _ = std::fs::rename(nw, &old_workspace);
            }
        }
        return Ok(RenameOutcome::Rejected(map_rename_registry_error(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            old_name,
            new_name,
            e,
            now,
        )?));
    }

    let _ = crate::identity::rename_project_membership_project(conn, old_name, new_name);

    if lifecycle::SLUG_RE.is_match(old_name) {
        let runtime_dir = sock_dir.join(old_name);
        if runtime_dir.is_dir() {
            let _ = std::fs::remove_dir_all(&runtime_dir);
        }
    }

    let alias_expires_at = registry
        .get(new_name)?
        .and_then(|row| row.aliases.into_iter().find(|a| a.name == old_name))
        .map(|a| a.expires_at)
        .unwrap_or_default();

    Ok(RenameOutcome::Renamed {
        from: old_name.to_string(),
        to: new_name.to_string(),
        grace_days,
        alias_expires_at,
    })
}

fn rename_token_files(token_dir: Option<&Path>, old_name: &str, new_name: &str) {
    let Some(dir) = token_dir else { return };
    if !dir.is_dir() {
        return;
    }
    let prefix = format!("{old_name}--");
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let fname = entry.file_name();
        let fname_str = fname.to_string_lossy();
        if let Some(suffix) = fname_str.strip_prefix(&prefix) {
            if fname_str.ends_with(".token") {
                let _ = std::fs::rename(entry.path(), dir.join(format!("{new_name}--{suffix}")));
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn map_rename_registry_error(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    old_name: &str,
    new_name: &str,
    e: RegistryError,
    now: DateTime<Utc>,
) -> Result<HandlerResponse, GateError> {
    Ok(match e {
        RegistryError::UnknownProject(_) => lifecycle::error_envelope(
            LifecycleError::NotRegistered,
            &format!("unknown project: {old_name:?}"),
            None,
        ),
        RegistryError::ProjectNameTaken(_) => name_collision_response(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            new_name,
            "project name is already registered",
        )?,
        RegistryError::AliasCollision(_) => {
            alias_collision_response(conn, registry, is_sysadmin, caller_user_id, new_name, now)?
        }
        RegistryError::InvalidName(_) => lifecycle::error_envelope(
            LifecycleError::InvalidName,
            "new name is not a valid slug",
            None,
        ),
        RegistryError::InvalidArgument(_) | RegistryError::Io(_) | RegistryError::Json(_) => {
            lifecycle::error_envelope(LifecycleError::Internal, "registry rename failed", None)
        }
    })
}

// -- Shared response builders (R1-F1/R2-F1 escape hatch, repeated at
// every collision-confirming call site above) ------------------------

fn forbidden_response(project_name: &str, role: &str, min_role: &str) -> HandlerResponse {
    lifecycle::error_envelope(
        LifecycleError::Forbidden,
        &format!(
            "operator holds only {role:?} membership on project {project_name:?}; \
             this action requires at least {min_role:?}-tier membership"
        ),
        None,
    )
}

fn active_sessions_response(name: &str, conns: u32) -> HandlerResponse {
    lifecycle::error_envelope(
        LifecycleError::ActiveSessions,
        &format!("{name:?} has {conns} active connection(s); disconnect them and retry"),
        Some(serde_json::json!({"active_connections": conns, "agents": []})),
    )
}

/// A name-collision confirmed against a REAL registered project --
/// R1-F1's escape hatch: a visible collision gets the rich message,
/// a hidden one gets the uniform not-found so a delegate can't
/// enumerate hidden tenants via the 409-vs-404 differential.
fn name_collision_response(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    colliding_name: &str,
    message: &str,
) -> Result<HandlerResponse, GateError> {
    Ok(
        match deny_cross_tenant_project_read(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            colliding_name,
            None,
        )? {
            CrossTenantOutcome::Admit => {
                lifecycle::error_envelope(LifecycleError::NameTaken, message, None)
            }
            _ => lifecycle::error_envelope(
                LifecycleError::NotFound,
                &format!("unknown project: {colliding_name:?}"),
                None,
            ),
        },
    )
}

/// Same escape hatch as [`name_collision_response`], for an alias
/// collision -- gated against the alias's real OWNER project, since
/// that's what confirms existence for a collision discovered via
/// alias rather than a direct name hit. Falls back to the plain
/// (visible) alias-collision message if the alias somehow no longer
/// resolves (a benign race between the caller's own two lookups).
fn alias_collision_response(
    conn: &Connection,
    registry: &ProjectRegistry,
    is_sysadmin: bool,
    caller_user_id: Option<&str>,
    new_name: &str,
    now: DateTime<Utc>,
) -> Result<HandlerResponse, GateError> {
    let message = format!("name {new_name:?} is an active alias");
    let Some(alias_owner) = registry.resolve_alias(new_name, now)? else {
        return Ok(lifecycle::error_envelope(
            LifecycleError::AliasCollision,
            &message,
            None,
        ));
    };
    Ok(
        match deny_cross_tenant_project_read(
            conn,
            registry,
            is_sysadmin,
            caller_user_id,
            &alias_owner,
            None,
        )? {
            CrossTenantOutcome::Admit => {
                lifecycle::error_envelope(LifecycleError::AliasCollision, &message, None)
            }
            _ => lifecycle::error_envelope(
                LifecycleError::NotFound,
                &format!("unknown project: {new_name:?}"),
                None,
            ),
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity;
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

    fn seed_operator_member(
        c: &mut Connection,
        username: &str,
        project: &str,
        role: &str,
    ) -> String {
        let uid = identity::create_user(
            c,
            username,
            "correct horse battery staple",
            None,
            false,
            true,
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

    // -- rename_precheck --------------------------------------------------

    #[test]
    fn precheck_proceeds_for_a_valid_rename() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(14)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Proceed(ok) = outcome else {
            panic!("expected Proceed, got {outcome:?}");
        };
        assert_eq!(ok.old_name, "old-name");
        assert_eq!(ok.new_name, "new-name");
        assert_eq!(ok.grace_days, 14);
    }

    #[test]
    fn precheck_defaults_grace_days_to_thirty() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Proceed(ok) = outcome else {
            panic!("expected Proceed");
        };
        assert_eq!(ok.grace_days, 30);
    }

    #[test]
    fn precheck_rejects_grace_days_out_of_range() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(99999)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn precheck_rejects_a_non_finite_grace_days_before_any_destructive_step() {
        // PF-R18-1: int(float('inf')) raises OverflowError in Python;
        // this must 400 too, not panic or silently wrap.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let huge: f64 = f64::INFINITY; // what a JSON `1e400` token parses to
        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(huge)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn precheck_accepts_grace_days_at_the_upper_bound_3650() {
        // test_sec_r2_grace_days_bounds.py's `test_grace_days_upper_
        // bound_ok`: 3650 is a legitimate max grace, not off-by-one
        // rejected.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(3650)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Proceed(ok) = outcome else {
            panic!("expected Proceed, got {outcome:?}");
        };
        assert_eq!(ok.grace_days, 3650);
    }

    #[test]
    fn precheck_rejects_a_negative_grace_days() {
        // test_sec_r2_grace_days_bounds.py's `test_grace_days_negative_
        // returns_400`.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(-1)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn precheck_rejects_the_literal_sec_round_2_repro_value_of_10_pow_18() {
        // test_sec_r2_grace_days_bounds.py's exact repro: `10 ** 18`
        // fits in an i64 (unlike PF-R18-1's `1e400`/infinity sibling
        // above), so this exercises the plain `0..=3650` range guard
        // rather than the non-finite-float guard -- both must 400.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let huge: i64 = 1_000_000_000_000_000_000;
        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            Some(&serde_json::json!(huge)),
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn precheck_rejects_a_non_string_new_name() {
        // test_sec_r8_type_confusion.py's rename half (PF-R8-1): a
        // structured JSON value for `name` must 400 via `reject_non_
        // str_name`, never reach `.strip()`/`.trim()`.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!({"nested": "obj"})),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 400);
    }

    #[test]
    fn precheck_rejects_identical_old_and_new_names() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("proj-a", "/ws/proj-a", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "proj-a", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "proj-a",
            Some(&serde_json::json!("proj-a")),
            None,
            now_dt(),
        )
        .unwrap();
        assert!(matches!(outcome, RenamePrecheck::Rejected(_)));
    }

    #[test]
    fn precheck_closes_the_oracle_on_a_hidden_name_collision() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        registry
            .register("hidden", "/ws/hidden", "python", now_dt())
            .unwrap();
        // bob is a member of old-name (can rename it) but NOT of "hidden".
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("hidden")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(
            resp.status, 404,
            "a hidden collision must look like unknown, not a confirmed 409"
        );
    }

    #[test]
    fn precheck_closes_the_oracle_on_a_hidden_alias_collision() {
        // The alias half of `precheck_closes_the_oracle_on_a_hidden_name_
        // collision` above (test_sec_r1f1_create_rename_name_oracle.py's
        // `test_rename_delegate_without_membership_alias_collision_gets_
        // uniform_404`): a hidden project's ALIAS must gate identically
        // to a hidden project's real name.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        registry
            .register("hidden", "/ws/hidden", "python", now_dt())
            .unwrap();
        registry
            .add_alias("hidden", "alias-of-hidden", None, Some(30), now_dt())
            .unwrap();
        // bob is a member of old-name (can rename it) but NOT of "hidden".
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("alias-of-hidden")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(
            resp.status, 404,
            "a hidden alias collision must look like unknown, not a confirmed 409"
        );
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "not_found");
    }

    #[test]
    fn precheck_denies_a_viewer_tier_member_as_forbidden() {
        // test_sec_r9f2_lifecycle_role_rank.py's rename half: a mere
        // `viewer`-tier member (even holding the deployment-wide
        // capability elsewhere) must not reach a destructive rename --
        // 403 forbidden, distinct from the 404 a genuine non-member sees.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "viewer");
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 403);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "forbidden");
    }

    #[test]
    fn precheck_denies_when_the_old_project_has_active_connections() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();
        store.with_runtime_mut("old-name", |rt| rt.active_conns = 1);

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            Some(&serde_json::json!("new-name")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
    }

    #[test]
    fn precheck_rejects_renaming_onto_a_visible_registered_project_as_409() {
        // test_sec_r37_rename_error_mapping.py's
        // `test_rename_to_existing_project_name_returns_409`: a plain,
        // non-racing rename onto an already-registered VISIBLE project
        // name must 409 name_taken, never a 500.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("alpha", "/ws/alpha", "python", now_dt())
            .unwrap();
        registry
            .register("beta", "/ws/beta", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "alpha", "operator");
        // bob must also see "beta" for the R1-F1 escape hatch to
        // surface the rich 409 rather than the hidden-collision 404.
        c.execute(
            "INSERT INTO project_membership (project_name, user_id, role) VALUES ('beta', ?1, 'operator')",
            [&uid],
        )
        .unwrap();
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "alpha",
            Some(&serde_json::json!("beta")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "name_taken");
    }

    #[test]
    fn precheck_rejects_an_unknown_old_name_as_404() {
        // test_sec_r37_rename_error_mapping.py's
        // `test_rename_unknown_old_name_returns_404`.
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();

        let outcome = rename_precheck(
            &c,
            &registry,
            &store,
            true,
            Some("u1"),
            "ghostproject",
            Some(&serde_json::json!("whatever")),
            None,
            now_dt(),
        )
        .unwrap();
        let RenamePrecheck::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "not_registered");
    }

    // -- rename_toctou_recheck --------------------------------------------

    #[test]
    fn toctou_recheck_proceeds_when_nothing_changed() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("old-name", "/ws/old-name", "python", now_dt())
            .unwrap();
        let store = RuntimeStore::default();

        let outcome = rename_toctou_recheck(
            &c,
            &registry,
            &store,
            false,
            Some("u1"),
            "old-name",
            "new-name",
            now_dt(),
        )
        .unwrap();
        assert!(matches!(outcome, RenameToctou::Proceed(_)));
    }

    #[test]
    fn toctou_recheck_denies_if_the_project_vanished_mid_flight() {
        let c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let store = RuntimeStore::default();

        let outcome = rename_toctou_recheck(
            &c,
            &registry,
            &store,
            false,
            Some("u1"),
            "old-name",
            "new-name",
            now_dt(),
        )
        .unwrap();
        let RenameToctou::Rejected(resp) = outcome else {
            panic!("expected Rejected");
        };
        assert_eq!(resp.status, 404);
    }

    #[test]
    fn toctou_recheck_denies_if_new_name_became_an_active_alias_mid_flight() {
        // PF-R36-1's alias-collision half (test_sec_r36_lifecycle_
        // parity.py's `test_rename_revalidates_alias_collision_inside_
        // lock`): if `new_name` became an active alias of ANOTHER
        // project between the outside-lock probe and the inside-lock
        // re-check, the destructive rename must never proceed.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("mover", "/ws/mover", "python", now_dt())
            .unwrap();
        registry
            .register("someoneelse", "/ws/someoneelse", "python", now_dt())
            .unwrap();
        registry
            .add_alias("someoneelse", "target", None, Some(30), now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "someoneelse", "operator");
        let store = RuntimeStore::default();

        let outcome = rename_toctou_recheck(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "mover",
            "target",
            now_dt(),
        )
        .unwrap();
        let RenameToctou::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "alias_collision");
    }

    // -- finish_rename_project --------------------------------------------

    #[test]
    fn finish_rename_project_moves_the_workspace_and_rekeys_membership() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let workspace_parent = dir.path().join("workspaces");
        std::fs::create_dir_all(&workspace_parent).unwrap();
        let old_workspace = workspace_parent.join("old-name");
        std::fs::create_dir_all(&old_workspace).unwrap();
        let row = registry
            .register(
                "old-name",
                old_workspace.to_str().unwrap(),
                "python",
                now_dt(),
            )
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();
        let sock_dir = dir.path().join("sockets");

        let outcome = finish_rename_project(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            "new-name",
            14,
            &row,
            &sock_dir,
            None,
            now_dt(),
        )
        .unwrap();
        let RenameOutcome::Renamed {
            from,
            to,
            grace_days,
            ..
        } = outcome
        else {
            panic!("expected Renamed, got {outcome:?}");
        };
        assert_eq!(from, "old-name");
        assert_eq!(to, "new-name");
        assert_eq!(grace_days, 14);

        assert!(!old_workspace.exists());
        assert!(workspace_parent.join("new-name").exists());
        assert!(registry.get("new-name").unwrap().is_some());
        let alias_owner = registry.resolve_alias("old-name", now_dt()).unwrap();
        assert_eq!(alias_owner.as_deref(), Some("new-name"));

        let rekeyed: String = c
            .query_row(
                "SELECT project_name FROM project_membership WHERE user_id = ?1",
                [&uid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rekeyed, "new-name");
    }

    #[test]
    fn finish_rename_project_leaves_a_non_conventional_workspace_untouched() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let custom_workspace = dir.path().join("custom-workspace-dir");
        std::fs::create_dir_all(&custom_workspace).unwrap();
        let row = registry
            .register(
                "old-name",
                custom_workspace.to_str().unwrap(),
                "python",
                now_dt(),
            )
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        let store = RuntimeStore::default();
        let sock_dir = dir.path().join("sockets");

        finish_rename_project(
            &c,
            &registry,
            &store,
            false,
            Some(&uid),
            "old-name",
            "new-name",
            30,
            &row,
            &sock_dir,
            None,
            now_dt(),
        )
        .unwrap();

        assert!(
            custom_workspace.exists(),
            "a non-conventional workspace path is never moved"
        );
        assert_eq!(
            registry.get("new-name").unwrap().unwrap().workspace,
            custom_workspace.to_string_lossy()
        );
    }

    #[test]
    fn finish_rename_project_rolls_back_the_workspace_move_on_a_registry_race() {
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        let workspace_parent = dir.path().join("workspaces");
        std::fs::create_dir_all(&workspace_parent).unwrap();
        let old_workspace = workspace_parent.join("old-name");
        std::fs::create_dir_all(&old_workspace).unwrap();
        let row = registry
            .register(
                "old-name",
                old_workspace.to_str().unwrap(),
                "python",
                now_dt(),
            )
            .unwrap();
        // A concurrent racer already registered the target name.
        registry
            .register("new-name", "/ws/somewhere-else", "python", now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "old-name", "operator");
        seed_operator_member(&mut c, "carol", "new-name", "operator");
        let store = RuntimeStore::default();
        let sock_dir = dir.path().join("sockets");

        // The caller must be able to SEE the colliding project for the
        // R1-F1 escape hatch to surface the rich 409 (a caller with no
        // visibility into "new-name" correctly gets the uniform 404
        // instead -- exercised separately by
        // `precheck_closes_the_oracle_on_a_hidden_name_collision`).
        // Sysadmin here isolates the ROLLBACK behavior this test is
        // actually about.
        let outcome = finish_rename_project(
            &c,
            &registry,
            &store,
            true,
            Some(&uid),
            "old-name",
            "new-name",
            30,
            &row,
            &sock_dir,
            None,
            now_dt(),
        )
        .unwrap();
        let RenameOutcome::Rejected(resp) = outcome else {
            panic!("expected Rejected, got {outcome:?}");
        };
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "name_taken");
        // The workspace move must have been rolled back.
        assert!(
            old_workspace.exists(),
            "workspace must be rolled back to its original location"
        );
        assert!(!workspace_parent.join("new-name").exists());
        assert!(
            registry.get("old-name").unwrap().is_some(),
            "old-name must still be registered"
        );
    }

    // -- map_rename_registry_error (R2-F1: the inside-lock exception-
    // handler backstop, confirmed live-race-reachable in Python since
    // `_ensure_lock` keys on old_name -- two renames with DIFFERENT
    // old-names racing the SAME new-name never serialise against each
    // other) -----------------------------------------------------------

    #[test]
    fn rename_backstop_project_name_taken_hidden_owner_gets_uniform_not_found() {
        // test_sec_r2f1_rename_toctou_backstop.py's `test_rename_
        // delegate_loses_project_name_taken_race_gets_uniform_404`: the
        // delegate has ZERO membership on the project that won the race
        // for `new_name` -- must see the SAME uniform 404 a genuinely
        // free name would, not the raw `name_taken` 409.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("shared-target", "/ws/shared-target", "python", now_dt())
            .unwrap();
        // bob has no membership on "shared-target" at all.
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

        let resp = map_rename_registry_error(
            &c,
            &registry,
            false,
            Some(&bob),
            "own-visible-project",
            "shared-target",
            RegistryError::ProjectNameTaken(
                "project 'shared-target' is already registered".to_string(),
            ),
            now_dt(),
        )
        .unwrap();
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "not_found");
        let text = serde_json::to_string(&body).unwrap().to_lowercase();
        assert!(!text.contains("taken"));
        assert!(!text.contains("already"));
    }

    #[test]
    fn rename_backstop_alias_collision_hidden_owner_gets_uniform_not_found() {
        // test_sec_r2f1_rename_toctou_backstop.py's `test_rename_
        // delegate_loses_alias_collision_race_gets_uniform_404`: the
        // winner is a concurrent `add_alias` on a HIDDEN project rather
        // than a create -- the delegate has no membership on the alias
        // OWNER either.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("hidden", "/ws/hidden", "python", now_dt())
            .unwrap();
        registry
            .add_alias("hidden", "collision-target", None, Some(30), now_dt())
            .unwrap();
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

        let resp = map_rename_registry_error(
            &c,
            &registry,
            false,
            Some(&bob),
            "own-visible-project",
            "collision-target",
            RegistryError::AliasCollision("name 'collision-target' is an active alias".to_string()),
            now_dt(),
        )
        .unwrap();
        assert_eq!(resp.status, 404);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "not_found");
        let text = serde_json::to_string(&body).unwrap().to_lowercase();
        assert!(!text.contains("alias"));
    }

    #[test]
    fn rename_backstop_alias_collision_visible_owner_gets_the_real_409() {
        // Happy path: the SAME race, but the loser can see the alias
        // owner (here, a real membership on it) -- must still get the
        // real, informative 409, matching PF-R37-1's original behaviour.
        let mut c = conn();
        let dir = tempfile::tempdir().unwrap();
        let registry = ProjectRegistry::new(dir.path().join("projects.local.json"));
        registry
            .register("visible", "/ws/visible", "python", now_dt())
            .unwrap();
        registry
            .add_alias("visible", "alias-target", None, Some(30), now_dt())
            .unwrap();
        let uid = seed_operator_member(&mut c, "bob", "visible", "operator");

        let resp = map_rename_registry_error(
            &c,
            &registry,
            false,
            Some(&uid),
            "own-visible-project",
            "alias-target",
            RegistryError::AliasCollision("name 'alias-target' is an active alias".to_string()),
            now_dt(),
        )
        .unwrap();
        assert_eq!(resp.status, 409);
        let HandlerBody::Json(body) = resp.body else {
            panic!("expected JSON body");
        };
        assert_eq!(body["error"], "alias_collision");
    }
}
