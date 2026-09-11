//! Boot sequence: create the project dir, init the per-project schema,
//! load the forwarding-HMAC key. Port of `agent_mcp/app/
//! server_lifecycle.py`'s steps 1-2 + `server_bootstrap.py`'s
//! `_load_forwarding_hmac_key` -- exact order, exact failure shape
//! (a hard `anyhow::bail!`/process exit on a directory/schema failure,
//! matching Python's `raise SystemExit`; a soft "dormant key" outcome
//! for every HMAC-key-loading failure mode, matching Python's own
//! defensive framing there).

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use conexus_db::migration::{Migrator, MigratorTrait};
use rusqlite::Connection;

/// `<project_dir>/.agent/mcp_state.db` -- the fixed per-project DB
/// path, matching Python's own layout.
pub fn db_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".agent").join("mcp_state.db")
}

/// Steps 1-2 of `server_lifecycle.py::initialize_server_state`:
/// create `project_dir` (parents included) then `.agent/` inside it.
/// A create failure is fatal -- matches Python's `raise SystemExit`.
pub fn ensure_project_dirs(project_dir: &Path) -> Result<()> {
    fs::create_dir_all(project_dir)
        .with_context(|| format!("create project directory {}", project_dir.display()))?;
    if !project_dir.is_dir() {
        anyhow::bail!(
            "project path '{}' is not a directory",
            project_dir.display()
        );
    }
    let agent_dir = project_dir.join(".agent");
    fs::create_dir_all(&agent_dir)
        .with_context(|| format!("initialize .agent directory at {}", agent_dir.display()))?;
    Ok(())
}

/// Open (creating if absent) the per-project DB. Schema authority is
/// [`apply_baseline_migration`] (sea-orm-migration's `Migrator`), run
/// separately once a `sea_orm::DatabaseConnection` onto this same file
/// exists (see `main.rs`'s real boot sequence, which opens that
/// connection right after this one) -- Phase F's schema-authority
/// cutover, replacing Alembic for real production databases. This
/// function itself no longer runs `conexus_db::schema::init_schema`:
/// that hand-written DDL is Rust-tests-only and already knowingly
/// behind the sea-orm-migration baseline (missing `mcp_sessions`, 3
/// FKs, DESC index ordering -- see the baseline migration's own module
/// doc for the full list of gaps it closed).
pub fn open_and_init_db(project_dir: &Path) -> Result<Connection> {
    let path = db_path(project_dir);
    Connection::open(&path).with_context(|| format!("open project database {}", path.display()))
}

/// Apply the real schema-authority baseline against `sea_orm_db` (the
/// same file [`open_and_init_db`] just opened/created). A no-op
/// against an already-migrated database -- whether migrated
/// historically by Alembic and adopted via `conexus-cli seed-baseline`,
/// or by a prior run of this exact function -- and creates the
/// complete current-HEAD schema (including the 3 FKs/`mcp_sessions`/
/// DESC-ordered indexes `schema::init_schema` never had) for a
/// genuinely fresh project.
pub async fn apply_baseline_migration(sea_orm_db: &sea_orm::DatabaseConnection) -> Result<()> {
    Migrator::up(sea_orm_db, None)
        .await
        .context("apply the sea-orm-migration schema-authority baseline")
}

/// Port of `server_bootstrap.py::_load_forwarding_hmac_key`. `path`
/// mirrors `--forwarding-hmac-in` (`None` when unset). Every failure
/// mode (missing flag, unreadable file, empty file) resolves to
/// `Ok(None)` -- a dormant key is not a boot failure, matching
/// Python's own "should not crash boot" framing; only the read
/// itself is fallible in the type signature, for the caller to log.
///
/// F015 v7: the file is read RAW, no `.strip()`/trim. It is 32 binary
/// bytes of `/dev/urandom`; any of those bytes can legitimately be
/// ASCII whitespace, and stripping them silently shortens the key
/// against what the router actually signed with -- the real historical
/// bug (a leading `\n` byte) that made every forwarding-header verify
/// fail. Never add a `.trim()`/`.strip()` here.
pub fn load_forwarding_hmac_key(path: Option<&Path>) -> Option<Vec<u8>> {
    let path = path?;
    let data = fs::read(path).ok()?;
    if data.is_empty() {
        return None;
    }
    Some(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ensure_project_dirs_creates_project_dir_and_dot_agent() {
        let dir = tempfile::tempdir().unwrap();
        let project_dir = dir.path().join("nested").join("project");
        ensure_project_dirs(&project_dir).unwrap();
        assert!(project_dir.is_dir());
        assert!(project_dir.join(".agent").is_dir());
    }

    #[test]
    fn ensure_project_dirs_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        ensure_project_dirs(dir.path()).unwrap();
        ensure_project_dirs(dir.path()).unwrap();
    }

    #[test]
    fn open_and_init_db_creates_the_expected_path_with_no_schema_yet() {
        let dir = tempfile::tempdir().unwrap();
        ensure_project_dirs(dir.path()).unwrap();
        let conn = open_and_init_db(dir.path()).unwrap();
        assert!(db_path(dir.path()).is_file());
        // Schema authority moved to `apply_baseline_migration` (async,
        // sea-orm) -- this sync open step no longer creates any table.
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[tokio::test]
    async fn apply_baseline_migration_creates_the_full_schema_on_a_fresh_db() {
        let dir = tempfile::tempdir().unwrap();
        ensure_project_dirs(dir.path()).unwrap();
        open_and_init_db(dir.path()).unwrap();

        let sea_orm_db =
            sea_orm::Database::connect(format!("sqlite://{}", db_path(dir.path()).display()))
                .await
                .unwrap();
        apply_baseline_migration(&sea_orm_db).await.unwrap();

        // Confirms the gaps `schema::init_schema` had are actually
        // closed for a fresh project now that the migrator is the
        // real boot-time authority, not just documented as fixed in
        // the baseline migration's own tests.
        let conn = Connection::open(db_path(dir.path())).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='mcp_sessions'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn apply_baseline_migration_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        ensure_project_dirs(dir.path()).unwrap();
        open_and_init_db(dir.path()).unwrap();
        let sea_orm_db =
            sea_orm::Database::connect(format!("sqlite://{}", db_path(dir.path()).display()))
                .await
                .unwrap();
        apply_baseline_migration(&sea_orm_db).await.unwrap();
        // A second real boot against the same file must not error --
        // exactly the boot-time shape (every process start runs this).
        apply_baseline_migration(&sea_orm_db).await.unwrap();
    }

    #[test]
    fn load_forwarding_hmac_key_returns_none_when_path_is_none() {
        assert_eq!(load_forwarding_hmac_key(None), None);
    }

    #[test]
    fn load_forwarding_hmac_key_returns_none_for_an_empty_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("key");
        fs::write(&path, b"").unwrap();
        assert_eq!(load_forwarding_hmac_key(Some(&path)), None);
    }

    #[test]
    fn load_forwarding_hmac_key_returns_none_for_a_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("does-not-exist");
        assert_eq!(load_forwarding_hmac_key(Some(&path)), None);
    }

    #[test]
    fn load_forwarding_hmac_key_preserves_leading_whitespace_bytes_verbatim() {
        // F015 v7 regression guard: a leading \n (0x0a) byte, the real
        // historical failure, must survive untouched.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("key");
        let raw = b"\n\x01\x02\x03rest-of-key-bytes";
        fs::write(&path, raw).unwrap();
        assert_eq!(load_forwarding_hmac_key(Some(&path)).unwrap(), raw.to_vec());
    }
}
