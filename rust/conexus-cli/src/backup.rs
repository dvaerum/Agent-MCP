//! `conexus-cli backup` -- port of `agent_mcp/cli.py`'s `backup_cmd`.
//! Online SQLite backup via `sqlite3.Connection.backup()`'s Rust
//! equivalent (`rusqlite::backup`) -- safe to run while the server is
//! live under WAL mode; readers and writers keep going.

use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::backup::Backup;
use rusqlite::Connection;

/// The project's live database path -- pure, no I/O, independently
/// testable (matches Python's `Path(project_dir).resolve() / ".agent"
/// / "mcp_state.db"`, minus the `.resolve()` canonicalization, which
/// the real filesystem check in [`run`] performs implicitly).
pub fn db_path_for(project_dir: &Path) -> PathBuf {
    project_dir.join(".agent").join("mcp_state.db")
}

pub fn run(project_dir: &Path, output_path: &Path, force: bool) -> anyhow::Result<()> {
    let src_path = db_path_for(
        &project_dir
            .canonicalize()
            .unwrap_or_else(|_| project_dir.to_path_buf()),
    );

    if !src_path.exists() {
        anyhow::bail!("database not found at {}", src_path.display());
    }
    if output_path.exists() {
        if !force {
            anyhow::bail!(
                "output file {} already exists; pass --force to overwrite",
                output_path.display()
            );
        }
        // Remove first (matching Python) so the backup writes a fresh
        // DB rather than appending pages to an unrelated sqlite file.
        std::fs::remove_file(output_path)?;
    }
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let src = Connection::open(&src_path)?;
    let mut dst = Connection::open(output_path)?;
    {
        let backup = Backup::new(&src, &mut dst)?;
        // The whole DB in one step (matching Python's
        // `sqlite3.Connection.backup()` default of copying everything
        // at once) -- this is an offline-scale utility, not a
        // live-server hot path, so there's no reason to chunk it into
        // steps with pauses. `i32::MAX` pages is effectively
        // "unbounded" for any real project database; unlike the raw
        // SQLite C API, rusqlite's own wrapper requires a POSITIVE
        // page count here (`-1`, the sqlite3 CLI's own "all pages"
        // sentinel, panics against this API) -- caught by a genuinely
        // failing test before assuming the sentinel would carry over.
        backup.run_to_completion(i32::MAX, Duration::from_millis(0), None)?;
    }
    println!("Backup complete: {}", output_path.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn db_path_for_joins_the_agent_state_db_path() {
        let path = db_path_for(Path::new("/srv/my-project"));
        assert_eq!(path, PathBuf::from("/srv/my-project/.agent/mcp_state.db"));
    }

    fn scratch_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "conexus-cli-backup-test-{name}-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join(".agent")).unwrap();
        dir
    }

    fn seed_db(project_dir: &Path) {
        let conn = Connection::open(db_path_for(project_dir)).unwrap();
        conn.execute_batch(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t (v) VALUES ('hello');",
        )
        .unwrap();
    }

    #[test]
    fn missing_source_database_is_a_clean_error() {
        let dir = scratch_dir("missing-src");
        let out = dir.join("out.db");
        let err = run(&dir, &out, false).unwrap_err();
        assert!(err.to_string().contains("database not found"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn an_existing_output_without_force_is_refused() {
        let dir = scratch_dir("no-force");
        seed_db(&dir);
        let out = dir.join("out.db");
        std::fs::write(&out, b"pretend-existing-backup").unwrap();

        let err = run(&dir, &out, false).unwrap_err();
        assert!(err.to_string().contains("already exists"));
        // The pre-existing file must be untouched, not clobbered.
        assert_eq!(std::fs::read(&out).unwrap(), b"pretend-existing-backup");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_real_backup_round_trips_the_data() {
        let dir = scratch_dir("real");
        seed_db(&dir);
        let out = dir.join("nested").join("out.db");

        run(&dir, &out, false).unwrap();

        let dst = Connection::open(&out).unwrap();
        let value: String = dst
            .query_row("SELECT v FROM t WHERE id = 1", [], |r| r.get(0))
            .unwrap();
        assert_eq!(value, "hello");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn force_overwrites_an_existing_output() {
        let dir = scratch_dir("force");
        seed_db(&dir);
        let out = dir.join("out.db");
        std::fs::write(&out, b"stale").unwrap();

        run(&dir, &out, true).unwrap();

        let dst = Connection::open(&out).unwrap();
        let value: String = dst
            .query_row("SELECT v FROM t WHERE id = 1", [], |r| r.get(0))
            .unwrap();
        assert_eq!(value, "hello");
        std::fs::remove_dir_all(&dir).ok();
    }
}
