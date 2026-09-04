//! Port of the `file_metadata` table's data-access surface (Python
//! has no standalone `file_metadata_repository.py` — `file_metadata_
//! tools.py` talks to the table directly via a raw cursor; this
//! module is the Rust equivalent seam, matching this crate's own
//! "one module per table" convention rather than inlining SQL into
//! the tool layer the way Python's newer code does).
//!
//! Per-file metadata captured by the indexer + file-lock tooling.
//! Keyed by normalized absolute filepath — one row per file. `metadata`
//! is an opaque JSON-as-TEXT blob (indexer-defined keys); this
//! repository never parses it, matching Python's ORM model doc
//! (`db/models/file_metadata.py`) and the ADR-0016 "dumb CRUD, no
//! schema awareness" precedent already established for
//! `project_settings_repository`/`project_context_repository`.
//! `content_hash` is a RAG-indexer concern (skip re-embedding
//! unchanged content) this repository only stores/returns, never
//! computes.

use rusqlite::{Connection, OptionalExtension, Result};

/// One row of the `file_metadata` table.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct FileMetadataRow {
    pub filepath: String,
    pub metadata: String,
    pub last_updated: String,
    pub updated_by: String,
    pub content_hash: Option<String>,
}

const COLUMNS: &str = "filepath, metadata, last_updated, updated_by, content_hash";

/// The recorded metadata for `filepath`, or `None` if nothing has
/// ever been set — the normal, benign state (metadata is optional and
/// operator-managed), not a missing-resource error.
pub fn get(conn: &Connection, filepath: &str) -> Result<Option<FileMetadataRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM file_metadata WHERE filepath = ?1"),
        [filepath],
        |row| {
            Ok(FileMetadataRow {
                filepath: row.get(0)?,
                metadata: row.get(1)?,
                last_updated: row.get(2)?,
                updated_by: row.get(3)?,
                content_hash: row.get(4)?,
            })
        },
    )
    .optional()
}

/// Insert or wholesale-replace `filepath`'s metadata row — Python's
/// `INSERT OR REPLACE INTO file_metadata (...)`. Replaces the ENTIRE
/// row (including `content_hash`, reset to `NULL` on every call from
/// the tool layer, matching Python's own unconditional-replace
/// semantic — this is a full overwrite, not a partial-field merge
/// like `project_settings_repository::upsert`'s BL-R22-1 rule).
pub fn upsert(
    conn: &Connection,
    filepath: &str,
    metadata: &str,
    updated_by: &str,
    now: &str,
) -> Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO file_metadata (filepath, metadata, last_updated, updated_by) \
         VALUES (?1, ?2, ?3, ?4)",
        (filepath, metadata, now, updated_by),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    fn conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        init_schema(&c).unwrap();
        c
    }

    #[test]
    fn get_on_an_unrecorded_path_returns_none() {
        let c = conn();
        assert_eq!(get(&c, "/tmp/a.rs").unwrap(), None);
    }

    #[test]
    fn upsert_then_get_returns_the_recorded_row() {
        let c = conn();
        upsert(
            &c,
            "/tmp/a.rs",
            r#"{"lang":"rust"}"#,
            "alice",
            "2026-06-01T00:00:00Z",
        )
        .unwrap();
        let row = get(&c, "/tmp/a.rs").unwrap().unwrap();
        assert_eq!(row.filepath, "/tmp/a.rs");
        assert_eq!(row.metadata, r#"{"lang":"rust"}"#);
        assert_eq!(row.updated_by, "alice");
        assert_eq!(row.last_updated, "2026-06-01T00:00:00Z");
        assert_eq!(row.content_hash, None);
    }

    #[test]
    fn a_second_upsert_replaces_the_whole_row() {
        let c = conn();
        upsert(
            &c,
            "/tmp/a.rs",
            r#"{"lang":"rust"}"#,
            "alice",
            "2026-06-01T00:00:00Z",
        )
        .unwrap();
        upsert(
            &c,
            "/tmp/a.rs",
            r#"{"lang":"python"}"#,
            "bob",
            "2026-06-01T00:01:00Z",
        )
        .unwrap();
        let row = get(&c, "/tmp/a.rs").unwrap().unwrap();
        assert_eq!(row.metadata, r#"{"lang":"python"}"#);
        assert_eq!(row.updated_by, "bob");
    }
}
