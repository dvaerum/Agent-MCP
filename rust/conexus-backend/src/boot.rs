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

/// Open (creating if absent) the per-project DB and apply this crate's
/// schema. Alembic stays the authoritative migration owner for a REAL
/// project DB until every Python backend is decommissioned (Phase F,
/// see `conexus_db::schema`'s own module doc) -- `init_schema` here is
/// `CREATE TABLE IF NOT EXISTS`, a no-op against an already-migrated
/// database, exactly like Python's own `initialize_database_schema()`
/// and `run_migrations_upgrade()` pair being idempotent against a
/// current DB.
pub fn open_and_init_db(project_dir: &Path) -> Result<Connection> {
    let path = db_path(project_dir);
    let conn = Connection::open(&path)
        .with_context(|| format!("open project database {}", path.display()))?;
    conexus_db::schema::init_schema(&conn)
        .with_context(|| format!("initialize schema at {}", path.display()))?;
    Ok(conn)
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
    fn open_and_init_db_creates_the_expected_path_and_schema() {
        let dir = tempfile::tempdir().unwrap();
        ensure_project_dirs(dir.path()).unwrap();
        let conn = open_and_init_db(dir.path()).unwrap();
        assert!(db_path(dir.path()).is_file());
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='project_settings'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
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
