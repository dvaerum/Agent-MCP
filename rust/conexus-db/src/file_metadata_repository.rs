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
//!
//! Phase G (sea-orm migration): the second repository converted, per
//! the plan's own real-call-site-count ordering (this table has 3
//! production call sites -- `file_metadata_tools.rs`'s `get`/`upsert`,
//! `rest_handlers.rs`'s `list_bounded` for `/api/all-data` -- all
//! already inside async contexts with `sea_orm_db` already threaded
//! through them from Phase G PR 2a's infra).

use sea_orm::{ActiveValue::Set, DatabaseConnection, DbErr, EntityTrait, QueryOrder, QuerySelect};

pub use crate::entity::file_metadata::Model as FileMetadataRow;
use crate::entity::file_metadata::{ActiveModel, Column, Entity};

/// The recorded metadata for `filepath`, or `None` if nothing has
/// ever been set — the normal, benign state (metadata is optional and
/// operator-managed), not a missing-resource error.
pub async fn get(
    db: &DatabaseConnection,
    filepath: &str,
) -> Result<Option<FileMetadataRow>, DbErr> {
    Entity::find_by_id(filepath.to_string()).one(db).await
}

/// The first `limit` rows, in whatever order SQLite returns them --
/// matches Python's `SELECT * FROM file_metadata LIMIT ?` exactly
/// (`/api/all-data`'s file_metadata section), which carries no
/// `ORDER BY` of its own either. A bounded read (pentest R3-F3's
/// "db review item 2" note: this was unbounded before), not a
/// recency-sorted one -- unlike `project_context_repository::
/// list_recent`, there's no Python `ORDER BY updated_at DESC` to
/// preserve here. Ordered by the primary key here only because
/// sea-orm's `Paginator`/`limit` needs SOME deterministic row source
/// to page against; SQLite's own unordered scan order (what the
/// original rusqlite `LIMIT`-with-no-`ORDER BY` actually returned) is
/// not otherwise observable through sea-orm's query builder, and no
/// caller depends on a specific order (confirmed by reading
/// `rest_handlers.rs`'s own call site, which never sorts the result).
pub async fn list_bounded(
    db: &DatabaseConnection,
    limit: i64,
) -> Result<Vec<FileMetadataRow>, DbErr> {
    Entity::find()
        .order_by_asc(Column::Filepath)
        .limit(limit as u64)
        .all(db)
        .await
}

/// Insert or wholesale-replace `filepath`'s metadata row — Python's
/// `INSERT OR REPLACE INTO file_metadata (...)`. Replaces the ENTIRE
/// row (including `content_hash`, reset to `NULL` on every call from
/// the tool layer, matching Python's own unconditional-replace
/// semantic — this is a full overwrite, not a partial-field merge
/// like `project_settings_repository::upsert`'s BL-R22-1 rule).
pub async fn upsert(
    db: &DatabaseConnection,
    filepath: &str,
    metadata: &str,
    updated_by: &str,
    now: &str,
) -> Result<(), DbErr> {
    let am = ActiveModel {
        filepath: Set(filepath.to_string()),
        metadata: Set(metadata.to_string()),
        last_updated: Set(now.to_string()),
        updated_by: Set(updated_by.to_string()),
        content_hash: Set(None),
    };
    Entity::insert(am)
        .on_conflict(
            sea_orm::sea_query::OnConflict::column(Column::Filepath)
                .update_columns([
                    Column::Metadata,
                    Column::LastUpdated,
                    Column::UpdatedBy,
                    Column::ContentHash,
                ])
                .to_owned(),
        )
        .exec(db)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::Database;

    async fn conn() -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
        }
        let db = Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, db)
    }

    #[tokio::test]
    async fn get_on_an_unrecorded_path_returns_none() {
        let (_dir, db) = conn().await;
        assert_eq!(get(&db, "/tmp/a.rs").await.unwrap(), None);
    }

    #[tokio::test]
    async fn upsert_then_get_returns_the_recorded_row() {
        let (_dir, db) = conn().await;
        upsert(
            &db,
            "/tmp/a.rs",
            r#"{"lang":"rust"}"#,
            "alice",
            "2026-06-01T00:00:00Z",
        )
        .await
        .unwrap();
        let row = get(&db, "/tmp/a.rs").await.unwrap().unwrap();
        assert_eq!(row.filepath, "/tmp/a.rs");
        assert_eq!(row.metadata, r#"{"lang":"rust"}"#);
        assert_eq!(row.updated_by, "alice");
        assert_eq!(row.last_updated, "2026-06-01T00:00:00Z");
        assert_eq!(row.content_hash, None);
    }

    #[tokio::test]
    async fn list_bounded_respects_the_limit() {
        let (_dir, db) = conn().await;
        upsert(&db, "/tmp/a.rs", "{}", "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        upsert(&db, "/tmp/b.rs", "{}", "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();
        upsert(&db, "/tmp/c.rs", "{}", "alice", "2026-01-01T00:00:00Z")
            .await
            .unwrap();

        assert_eq!(list_bounded(&db, 10).await.unwrap().len(), 3);
        assert_eq!(list_bounded(&db, 2).await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn a_second_upsert_replaces_the_whole_row() {
        let (_dir, db) = conn().await;
        upsert(
            &db,
            "/tmp/a.rs",
            r#"{"lang":"rust"}"#,
            "alice",
            "2026-06-01T00:00:00Z",
        )
        .await
        .unwrap();
        upsert(
            &db,
            "/tmp/a.rs",
            r#"{"lang":"python"}"#,
            "bob",
            "2026-06-01T00:01:00Z",
        )
        .await
        .unwrap();
        let row = get(&db, "/tmp/a.rs").await.unwrap().unwrap();
        assert_eq!(row.metadata, r#"{"lang":"python"}"#);
        assert_eq!(row.updated_by, "bob");
    }
}
