//! Port of `agent_mcp/repositories/rag_repository.py`.
//!
//! A module of plain functions — Python's version is a class, but its
//! own docstring states instances are stateless and deliberately hold
//! no cache ("vector search is itself a kind of cache; an additional
//! Python-side layer would just complicate invalidation"). With
//! nothing to hold, there's no reason for a wrapper type here either,
//! matching this crate's established rule (`project_context_repository`,
//! `project_settings_repository`).
//!
//! ## ADR-0017: no content-based redaction here
//! `search_similar`/`fetch_recent_context` return rows AS-IS.
//! Protection is by authorization (RAG is per-project-scoped), not by
//! sniffing content for secrets — do not "helpfully" add scanning to
//! this seam.
//!
//! ## Degrade contract, simplified from Python
//! Every mutation/search function here checks ONLY whether
//! `rag_embeddings` exists in `sqlite_master` (`embeddings_table_exists`)
//! — Python ALSO checks a separate process-global "was the extension
//! successfully loaded" flag (`g.global_vss_load_successful`), because
//! its per-connection `sqlite_vec.load(conn)` pattern means the
//! virtual table can be registered in schema while not actually
//! usable on a specific connection. That gap doesn't exist here:
//! `conexus_vec`'s loader registers the extension process-wide via
//! `sqlite3_auto_extension`, so once registered, EVERY connection
//! gets it — table-existence alone is a sufficient and simpler check.
//! This also means `purge_source`'s Python counterpart swallowing a
//! `sqlite3.OperationalError` from a "registered but not loaded"
//! embeddings delete has no equivalent failure mode to port here.
//!
//! ## Errors, diverging from Python on purpose
//! Real DB errors propagate as `Err` here, consistent with every
//! other repository in this crate — Python collapses DB errors and
//! "table absent" into the same empty-shaped sentinel (`[]`/`None`/
//! `0`), which hides genuine failures from callers. The ONE sentinel
//! kept as a deliberate `Ok` (not an error) is "no embeddings table" →
//! empty results: that's the intended graceful-degrade-to-no-RAG
//! behavior from Phase A's `conexus-vec`, not an error condition.
//!
//! ## rowid-linkage invariant
//! `rag_chunks.chunk_id == rag_embeddings.rowid` is the join key
//! sqlite-vec's `vec0` uses. Delete ordering matters: embeddings
//! before chunks, since the sub-select needs the chunk rows to still
//! exist to resolve which rowids to purge.
//!
//! ## `purge_source` vs `delete_chunks_for`
//! `purge_source` ALSO clears the `hash_<type>_<ref>` watermark (used
//! on hard entity deletion, so a future re-add re-indexes instead of
//! being skipped as "unchanged" against a ghost hash); `delete_chunks_for`
//! must NOT clear it — it's used mid-indexer-cycle, which re-inserts
//! the chunk and re-sets the hash in the same cycle. Conflating these
//! breaks the incremental indexer's re-index detection.
//!
//! ## R31 watermark contract (not enforced here, just plumbed)
//! `set_meta`/`get_last_indexed` are the mechanism a future indexer
//! uses to cap watermark advancement below any row that failed to
//! embed in a cycle — this module doesn't implement that policy
//! itself, only exposes the read/write primitives it needs.
//!
//! Phase G (sea-orm migration): every read + `embeddings_table_exists`
//! is sea-orm-backed. `bulk_index_chunks`/`delete_chunks_for`/
//! `set_meta` stay rusqlite-only, but NOT as a hot-path/transaction-
//! bound classification the way most of this crate's remaining sync
//! functions are — they have no real production caller anywhere in
//! the workspace today, since the R31 indexing loop
//! (`run_rag_indexing_periodically`) they exist to serve is itself
//! not yet ported (a still-open, un-answered operator question,
//! tracked separately from this repository's own conversion status).
//! Converting them now, with no real caller to convert alongside,
//! would violate this migration's own "convert callers together"
//! rule for lack of a caller to convert — revisit once that loop is
//! built. `purge_source` DOES stay permanently sync for the usual
//! reason: its two real callers (`task_tools::DeleteTaskTool`'s
//! cascade, `project_context_tools`'s delete path) both run it inside
//! or alongside an in-flight write, one of them a literal
//! `rusqlite::Transaction`.
//!
//! ## Phase G (sea-orm migration): reads converted, writes deliberately not
//! `embeddings_table_exists`/`get_last_indexed`/`get_all_meta`/
//! `get_chunk_by_id`/`fetch_recent_context`/`search_similar` are
//! rewritten here onto `sea_orm::DatabaseConnection`, via the
//! `rag_chunk`/`rag_meta` Entities plus a raw-SQL escape hatch for
//! `rag_embeddings` (a `vec0` virtual table sea-query's builder has no
//! `MATCH`/`k =` vocabulary for) and for `sqlite_master` introspection.
//! `bulk_index_chunks`/`delete_chunks_for`/`purge_source` stay
//! synchronous on `rusqlite::Connection` — each is called from inside
//! an already-established rusqlite transaction/connection at its own
//! real call site (`task_tools::DeleteTaskTool`'s cascade,
//! `project_context_tools::DeleteProjectContextTool`), so converting
//! them now would either break that call site's atomicity story or
//! require restructuring those tools onto sea-orm transactions —
//! out of scope for this slice. Since the write functions still need
//! an existence check and the public `embeddings_table_exists` above
//! changed shape to take a `DatabaseConnection`, they call the private
//! rusqlite-flavored [`embeddings_table_exists_sync`] instead — same
//! query, same semantics, just not the public (now-async) name.

use rusqlite::{Connection, OptionalExtension, Result};
use sea_orm::{
    ColumnTrait, ConnectionTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder,
    QuerySelect, Statement,
};
use std::collections::HashMap;

use crate::entity::{project_context, rag_chunk, rag_meta};

/// One row of the `rag_chunks` table. `metadata` is parsed from its
/// stored JSON text; malformed JSON degrades to `None` rather than an
/// error (matches Python: a chunk written before a metadata-shape
/// change shouldn't become unreadable).
#[derive(Debug, Clone, PartialEq)]
pub struct RagChunkRow {
    pub chunk_id: i64,
    pub source_type: String,
    pub source_ref: String,
    pub chunk_text: String,
    pub indexed_at: String,
    pub metadata: Option<serde_json::Value>,
}

/// A [`RagChunkRow`] plus its `vec0`-computed distance from the query
/// embedding, ascending (closest match first).
#[derive(Debug, Clone, PartialEq)]
pub struct RagSearchResult {
    pub chunk: RagChunkRow,
    pub distance: f64,
}

/// One `project_context` row as `fetch_recent_context` projects it —
/// deliberately narrower than the full `ProjectContextRow` (no
/// `created_at`/`created_by`), matching Python's own narrower
/// `SELECT`.
#[derive(Debug, Clone, PartialEq)]
pub struct RecentContextEntry {
    pub context_key: String,
    pub value: String,
    pub description: Option<String>,
    pub updated_at: String,
}

/// One chunk to ingest via [`bulk_index_chunks`]. `chunk_text` empty
/// means "skip this entry entirely" (matches Python — no row is
/// written at all, not even without an embedding).
pub struct NewChunk<'a> {
    pub chunk_text: &'a str,
    pub metadata: Option<&'a serde_json::Value>,
    pub embedding: Option<&'a [f32]>,
}

/// Whether the `rag_embeddings` vec0 table exists -- the degrade
/// contract's single source of truth (see this module's doc). `pub`
/// (Phase D2) so `ask_project_rag` can skip the embedding HTTP call
/// entirely when RAG isn't set up, mirroring Python's own
/// `is_vss_loadable()` pre-check in `query_rag_system` -- every other
/// function in this module already uses this same check internally,
/// so `ask_project_rag` calling it too doesn't introduce a second
/// notion of "is RAG available", just reuses the one that exists.
pub async fn embeddings_table_exists(db: &DatabaseConnection) -> Result<bool, DbErr> {
    // sea-query's builder has no `sqlite_master` introspection
    // vocabulary — same raw-SQL escape hatch as `search_similar`'s own
    // `vec0` query below.
    let stmt = Statement::from_string(
        sea_orm::DatabaseBackend::Sqlite,
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual') AND name = 'rag_embeddings'",
    );
    Ok(db.query_one_raw(stmt).await?.is_some())
}

/// Sync rusqlite duplicate of [`embeddings_table_exists`] — kept ONLY
/// for `bulk_index_chunks`/`delete_chunks_for`/`purge_source` below,
/// which stay on `rusqlite::Connection` (see this module's own doc)
/// and can no longer call the public async version once its signature
/// changed to `&sea_orm::DatabaseConnection`. Not `pub`: no external
/// caller needs a rusqlite-flavored check any more.
fn embeddings_table_exists_sync(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual') AND name = 'rag_embeddings'",
        [],
        |_| Ok(()),
    )
    .optional()
    .map(|found| found.is_some())
}

fn chunk_row_from_model(model: rag_chunk::Model) -> RagChunkRow {
    RagChunkRow {
        chunk_id: model.chunk_id,
        source_type: model.source_type,
        source_ref: model.source_ref,
        chunk_text: model.chunk_text,
        indexed_at: model.indexed_at,
        metadata: model.metadata.and_then(|s| serde_json::from_str(&s).ok()),
    }
}

const CHUNK_COLUMNS: &str = "chunk_id, source_type, source_ref, chunk_text, indexed_at, metadata";

/// Insert N chunks + their companion embeddings. Returns the count of
/// chunk rows actually written (empty-text chunks are skipped and
/// don't count). An embedding is written only when both the chunk
/// provides one AND `rag_embeddings` exists — otherwise the chunk row
/// still lands, its embedding silently skipped (the degrade path: a
/// host without sqlite-vec still gets full-text-searchable rows, just
/// not vector-searchable ones).
///
/// Deliberately NOT converted to sea-orm (Phase G): called from inside
/// an already-established rusqlite transaction in
/// `project_context_tools`'s own test helper today, and future real
/// (indexer) callers are expected to run inside a rusqlite transaction
/// too — see this module's own doc for why converting now is out of
/// scope.
pub fn bulk_index_chunks(
    conn: &Connection,
    source_type: &str,
    source_ref: &str,
    chunks: &[NewChunk],
    now_iso: &str,
) -> Result<i64> {
    let has_embeddings_table = embeddings_table_exists_sync(conn)?;
    let mut inserted = 0i64;

    for chunk in chunks {
        if chunk.chunk_text.is_empty() {
            continue;
        }
        let metadata_json = chunk.metadata.map(|m| m.to_string());
        conn.execute(
            "INSERT INTO rag_chunks (source_type, source_ref, chunk_text, indexed_at, metadata) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            (
                source_type,
                source_ref,
                chunk.chunk_text,
                now_iso,
                metadata_json,
            ),
        )?;
        let chunk_rowid = conn.last_insert_rowid();

        if has_embeddings_table {
            if let Some(embedding) = chunk.embedding {
                let embedding_json =
                    serde_json::to_string(embedding).expect("a &[f32] always serializes");
                conn.execute(
                    "INSERT INTO rag_embeddings (rowid, embedding) VALUES (?1, ?2)",
                    (chunk_rowid, embedding_json),
                )?;
            }
        }
        inserted += 1;
    }

    Ok(inserted)
}

/// Removes chunk rows + their embeddings for one source. Does NOT
/// touch the `hash_<type>_<ref>` watermark — see the module doc for
/// why that distinguishes this from [`purge_source`].
///
/// Deliberately NOT converted to sea-orm (Phase G): same reasoning as
/// [`bulk_index_chunks`] — this module's own doc explains why.
pub fn delete_chunks_for(conn: &Connection, source_type: &str, source_ref: &str) -> Result<i64> {
    if embeddings_table_exists_sync(conn)? {
        conn.execute(
            "DELETE FROM rag_embeddings WHERE rowid IN \
             (SELECT chunk_id FROM rag_chunks WHERE source_type = ?1 AND source_ref = ?2)",
            (source_type, source_ref),
        )?;
    }
    let deleted = conn.execute(
        "DELETE FROM rag_chunks WHERE source_type = ?1 AND source_ref = ?2",
        (source_type, source_ref),
    )?;
    Ok(deleted as i64)
}

/// Hard-evicts a source: chunks + embeddings + the `hash_<type>_<ref>`
/// watermark, so a future re-add re-indexes instead of being skipped
/// as "unchanged" against a ghost hash. Returns the chunk rows
/// deleted.
///
/// Deliberately NOT converted to sea-orm (Phase G): called from inside
/// already-established rusqlite transactions/connections in
/// `task_tools::DeleteTaskTool`'s cascade and `project_context_tools::
/// DeleteProjectContextTool` — converting it now would either break
/// that call site's atomicity story or require restructuring those
/// tools onto sea-orm transactions; see this module's own doc.
pub fn purge_source(conn: &Connection, source_type: &str, source_ref: &str) -> Result<i64> {
    if embeddings_table_exists_sync(conn)? {
        conn.execute(
            "DELETE FROM rag_embeddings WHERE rowid IN \
             (SELECT chunk_id FROM rag_chunks WHERE source_type = ?1 AND source_ref = ?2)",
            (source_type, source_ref),
        )?;
    }
    let deleted = conn.execute(
        "DELETE FROM rag_chunks WHERE source_type = ?1 AND source_ref = ?2",
        (source_type, source_ref),
    )?;

    let meta_key = format!("hash_{source_type}_{source_ref}");
    conn.execute("DELETE FROM rag_meta WHERE meta_key = ?1", [&meta_key])?;

    Ok(deleted as i64)
}

/// Writes `last_indexed_<source_type>` and/or `hash_<source_type>_<ref>`
/// rows. A no-op if both `last_indexed_at` and `source_hashes` are
/// `None`.
pub fn set_meta(
    conn: &Connection,
    source_type: &str,
    last_indexed_at: Option<&str>,
    source_hashes: Option<&[(&str, &str)]>,
) -> Result<()> {
    if let Some(v) = last_indexed_at {
        let key = format!("last_indexed_{source_type}");
        conn.execute(
            "INSERT OR REPLACE INTO rag_meta (meta_key, meta_value) VALUES (?1, ?2)",
            (key, v),
        )?;
    }
    if let Some(hashes) = source_hashes {
        for (source_ref, hash) in hashes {
            let key = format!("hash_{source_type}_{source_ref}");
            conn.execute(
                "INSERT OR REPLACE INTO rag_meta (meta_key, meta_value) VALUES (?1, ?2)",
                (key, hash),
            )?;
        }
    }
    Ok(())
}

pub async fn get_last_indexed(
    db: &DatabaseConnection,
    source_type: &str,
) -> Result<Option<String>, DbErr> {
    let key = format!("last_indexed_{source_type}");
    let row = rag_meta::Entity::find_by_id(key).one(db).await?;
    Ok(row.and_then(|m| m.meta_value))
}

/// Bulk read of every `rag_meta` row — the indexer's per-cycle
/// prelude. Rows with a `NULL` value are omitted (a `HashMap<String,
/// String>` has no way to represent one).
pub async fn get_all_meta(db: &DatabaseConnection) -> Result<HashMap<String, String>, DbErr> {
    let rows = rag_meta::Entity::find().all(db).await?;
    Ok(rows
        .into_iter()
        .filter_map(|m| {
            let rag_meta::Model {
                meta_key,
                meta_value,
            } = m;
            meta_value.map(|v| (meta_key, v))
        })
        .collect())
}

pub async fn get_chunk_by_id(
    db: &DatabaseConnection,
    chunk_id: i64,
) -> Result<Option<RagChunkRow>, DbErr> {
    let row = rag_chunk::Entity::find_by_id(chunk_id).one(db).await?;
    Ok(row.map(chunk_row_from_model))
}

/// K-nearest-neighbor search against `rag_embeddings`, joined back to
/// `rag_chunks` for the actual text/metadata (`vec0` only stores the
/// vector). Empty, not an error, when `rag_embeddings` doesn't exist.
///
/// `source_type_filter` is NOT pushed into `vec0`'s `WHERE` clause —
/// `vec0` only understands its own `MATCH`/`k` predicates there, so
/// filtering happens after fetch. To avoid starving `limit` under a
/// filter, this over-fetches `limit * 4` candidates before filtering
/// (matches Python's own heuristic multiplier).
///
/// Raw-SQL escape hatch, not a typed sea-orm query: `rag_embeddings`
/// is a sqlite-vec `vec0` virtual table with no Entity (sea-query's
/// builder has no vocabulary for `MATCH`/`k =` KNN syntax) — kept as
/// ONE prepared statement selecting the full `CHUNK_COLUMNS` set plus
/// `r.distance`, matching how the rusqlite version ran one prepared
/// statement too.
pub async fn search_similar(
    db: &DatabaseConnection,
    query_embedding: &[f32],
    limit: i64,
    source_type_filter: Option<&str>,
) -> Result<Vec<RagSearchResult>, DbErr> {
    if !embeddings_table_exists(db).await? {
        return Ok(Vec::new());
    }

    let effective_k = if source_type_filter.is_some() {
        limit * 4
    } else {
        limit
    };
    let query_json = serde_json::to_string(query_embedding).expect("a &[f32] always serializes");

    let sql = format!(
        "SELECT {}, r.distance FROM rag_embeddings r \
         JOIN rag_chunks c ON r.rowid = c.chunk_id \
         WHERE r.embedding MATCH ? AND k = ? ORDER BY r.distance",
        CHUNK_COLUMNS
            .split(", ")
            .map(|c| format!("c.{c}"))
            .collect::<Vec<_>>()
            .join(", ")
    );
    let stmt = Statement::from_sql_and_values(
        sea_orm::DatabaseBackend::Sqlite,
        &sql,
        [query_json.into(), effective_k.into()],
    );
    let rows = db.query_all_raw(stmt).await?;

    let mut results = Vec::new();
    for row in rows {
        let metadata_raw: Option<String> = row.try_get("", "metadata")?;
        let chunk = RagChunkRow {
            chunk_id: row.try_get("", "chunk_id")?,
            source_type: row.try_get("", "source_type")?,
            source_ref: row.try_get("", "source_ref")?,
            chunk_text: row.try_get("", "chunk_text")?,
            indexed_at: row.try_get("", "indexed_at")?,
            metadata: metadata_raw.and_then(|s| serde_json::from_str(&s).ok()),
        };
        let distance: f64 = row.try_get("", "distance")?;

        if let Some(filter) = source_type_filter {
            if chunk.source_type != filter {
                continue;
            }
        }
        results.push(RagSearchResult { chunk, distance });
        if results.len() as i64 >= limit {
            break;
        }
    }
    Ok(results)
}

/// Time-windowed "recently changed" `project_context` entries — reads
/// `project_context`, not any RAG table. `limit: None` drops the
/// `LIMIT` clause entirely (an unbounded read), matching a historical
/// Python call shape. A real sea-orm typed query (the already-merged
/// `project_context::Entity`, not a raw `Statement`) — unlike
/// `search_similar`, there's no `vec0`-shaped obstacle here.
pub async fn fetch_recent_context(
    db: &DatabaseConnection,
    since: &str,
    limit: Option<i64>,
) -> Result<Vec<RecentContextEntry>, DbErr> {
    let mut query = project_context::Entity::find()
        .filter(project_context::Column::UpdatedAt.gt(since))
        .order_by_desc(project_context::Column::UpdatedAt);
    if let Some(l) = limit {
        query = query.limit(l as u64);
    }
    let rows = query.all(db).await?;
    Ok(rows
        .into_iter()
        .map(|m| RecentContextEntry {
            context_key: m.context_key,
            value: m.value,
            description: m.description,
            updated_at: m.updated_at,
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{init_rag_embeddings_table, init_schema};
    use std::sync::Once;

    /// `register_sqlite_vec` is a real, process-wide, one-way
    /// registration (see `conexus-vec`) — safe to call more than
    /// once, but pointless to repeat per-test.
    static VEC_REGISTERED: Once = Once::new();

    fn test_conn_with_vec(dimension: u32) -> Connection {
        VEC_REGISTERED.call_once(|| {
            assert!(
                conexus_vec::register_sqlite_vec(),
                "sqlite-vec must be loadable in the test environment"
            );
        });
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        init_rag_embeddings_table(&conn, dimension).unwrap();
        conn
    }

    /// sea-orm needs a SEPARATE connection pool from rusqlite's, so a
    /// test seeding through `bulk_index_chunks`/`set_meta` (still
    /// rusqlite) and then reading through `get_chunk_by_id`/
    /// `get_last_indexed`/`get_all_meta`/`search_similar` (now
    /// sea-orm) needs a REAL FILE-BACKED temp DB, not `:memory:` —
    /// `:memory:` can't be shared across two connection handles. Same
    /// pattern as `task_repository::test_conn_with_sea_orm`.
    async fn test_conn_with_vec_and_sea_orm(
        dimension: u32,
    ) -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        VEC_REGISTERED.call_once(|| {
            assert!(
                conexus_vec::register_sqlite_vec(),
                "sqlite-vec must be loadable in the test environment"
            );
        });
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        init_rag_embeddings_table(&conn, dimension).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, conn, db)
    }

    /// Same file-backed rationale as [`test_conn_with_vec_and_sea_orm`],
    /// without the `rag_embeddings` vec0 table.
    async fn test_conn_without_vec_and_sea_orm(
    ) -> (tempfile::TempDir, Connection, sea_orm::DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, conn, db)
    }

    fn one_chunk<'a>(text: &'a str, embedding: Option<&'a [f32]>) -> NewChunk<'a> {
        NewChunk {
            chunk_text: text,
            metadata: None,
            embedding,
        }
    }

    #[tokio::test]
    async fn bulk_index_chunks_writes_chunk_and_embedding_rows() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        let embedding = [1.0f32, 0.0, 0.0];
        let chunks = vec![one_chunk("hello world", Some(&embedding))];

        let inserted = bulk_index_chunks(
            &conn,
            "markdown",
            "docs/readme.md",
            &chunks,
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(inserted, 1);

        let chunk = get_chunk_by_id(&db, 1).await.unwrap().unwrap();
        assert_eq!(chunk.source_type, "markdown");
        assert_eq!(chunk.source_ref, "docs/readme.md");
        assert_eq!(chunk.chunk_text, "hello world");

        // The embedding row must exist too (rowid == chunk_id).
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM rag_embeddings WHERE rowid = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn bulk_index_chunks_skips_empty_text_entirely() {
        let conn = test_conn_with_vec(3);
        let embedding = [1.0f32, 0.0, 0.0];
        let chunks = vec![
            one_chunk("", Some(&embedding)),
            one_chunk("real content", Some(&embedding)),
        ];

        let inserted =
            bulk_index_chunks(&conn, "markdown", "a", &chunks, "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(
            inserted, 1,
            "the empty-text entry must not count or write a row"
        );
    }

    #[tokio::test]
    async fn bulk_index_chunks_writes_chunk_row_even_without_an_embedding() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        let chunks = vec![one_chunk("no vector for this one", None)];

        let inserted =
            bulk_index_chunks(&conn, "markdown", "a", &chunks, "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(inserted, 1);
        assert!(get_chunk_by_id(&db, 1).await.unwrap().is_some());

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_embeddings", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            count, 0,
            "no embedding was provided, so none should have been written"
        );
    }

    #[tokio::test]
    async fn bulk_index_chunks_degrades_gracefully_without_an_embeddings_table() {
        let (_dir, conn, db) = test_conn_without_vec_and_sea_orm().await;
        let embedding = [1.0f32, 0.0, 0.0];
        let chunks = vec![one_chunk("still indexed as text", Some(&embedding))];

        let inserted =
            bulk_index_chunks(&conn, "markdown", "a", &chunks, "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(
            inserted, 1,
            "the chunk row must still land even with no rag_embeddings table"
        );
        assert!(get_chunk_by_id(&db, 1).await.unwrap().is_some());
    }

    #[test]
    fn delete_chunks_for_removes_chunks_and_embeddings_for_one_source_only() {
        let conn = test_conn_with_vec(3);
        let embedding = [1.0f32, 0.0, 0.0];
        bulk_index_chunks(
            &conn,
            "markdown",
            "a",
            &[one_chunk("a-content", Some(&embedding))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        bulk_index_chunks(
            &conn,
            "markdown",
            "b",
            &[one_chunk("b-content", Some(&embedding))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let deleted = delete_chunks_for(&conn, "markdown", "a").unwrap();
        assert_eq!(deleted, 1);

        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_chunks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining, 1, "source b's chunk must survive");
        let remaining_emb: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_embeddings", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining_emb, 1, "source b's embedding must survive");
    }

    #[test]
    fn delete_chunks_for_does_not_touch_the_hash_watermark() {
        let conn = test_conn_with_vec(3);
        set_meta(&conn, "markdown", None, Some(&[("a", "hash123")])).unwrap();
        bulk_index_chunks(
            &conn,
            "markdown",
            "a",
            &[one_chunk("content", None)],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        delete_chunks_for(&conn, "markdown", "a").unwrap();

        let hash: Option<String> = conn
            .query_row(
                "SELECT meta_value FROM rag_meta WHERE meta_key = 'hash_markdown_a'",
                [],
                |r| r.get(0),
            )
            .optional()
            .unwrap();
        assert_eq!(
            hash.as_deref(),
            Some("hash123"),
            "delete_chunks_for must NOT clear the watermark -- that's purge_source's job"
        );
    }

    #[test]
    fn purge_source_clears_chunks_embeddings_and_the_hash_watermark() {
        let conn = test_conn_with_vec(3);
        set_meta(&conn, "markdown", None, Some(&[("a", "hash123")])).unwrap();
        bulk_index_chunks(
            &conn,
            "markdown",
            "a",
            &[one_chunk("content", Some(&[1.0, 0.0, 0.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let deleted = purge_source(&conn, "markdown", "a").unwrap();
        assert_eq!(deleted, 1);

        let chunks: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_chunks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(chunks, 0);
        let embeddings: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_embeddings", [], |r| r.get(0))
            .unwrap();
        assert_eq!(embeddings, 0);
        let hash: Option<String> = conn
            .query_row(
                "SELECT meta_value FROM rag_meta WHERE meta_key = 'hash_markdown_a'",
                [],
                |r| r.get(0),
            )
            .optional()
            .unwrap();
        assert_eq!(
            hash, None,
            "purge_source MUST clear the watermark so a re-add re-indexes"
        );
    }

    #[tokio::test]
    async fn set_meta_and_get_last_indexed_round_trip() {
        let (_dir, conn, db) = test_conn_without_vec_and_sea_orm().await;
        assert_eq!(get_last_indexed(&db, "markdown").await.unwrap(), None);

        set_meta(&conn, "markdown", Some("2026-01-01T00:00:00Z"), None).unwrap();
        assert_eq!(
            get_last_indexed(&db, "markdown").await.unwrap().as_deref(),
            Some("2026-01-01T00:00:00Z")
        );

        // Re-writing (INSERT OR REPLACE) must overwrite, not conflict.
        set_meta(&conn, "markdown", Some("2026-01-02T00:00:00Z"), None).unwrap();
        assert_eq!(
            get_last_indexed(&db, "markdown").await.unwrap().as_deref(),
            Some("2026-01-02T00:00:00Z")
        );
    }

    #[tokio::test]
    async fn set_meta_writes_source_hashes_independently_of_last_indexed_at() {
        let (_dir, conn, db) = test_conn_without_vec_and_sea_orm().await;
        set_meta(
            &conn,
            "context",
            None,
            Some(&[("key-a", "hash-a"), ("key-b", "hash-b")]),
        )
        .unwrap();

        assert_eq!(
            get_last_indexed(&db, "context").await.unwrap(),
            None,
            "no last_indexed_at was given, so it must stay unwritten"
        );
        let all = get_all_meta(&db).await.unwrap();
        assert_eq!(
            all.get("hash_context_key-a").map(String::as_str),
            Some("hash-a")
        );
        assert_eq!(
            all.get("hash_context_key-b").map(String::as_str),
            Some("hash-b")
        );
    }

    #[tokio::test]
    async fn get_all_meta_returns_every_row() {
        let (_dir, conn, db) = test_conn_without_vec_and_sea_orm().await;
        set_meta(
            &conn,
            "markdown",
            Some("2026-01-01T00:00:00Z"),
            Some(&[("a", "h1")]),
        )
        .unwrap();

        let all = get_all_meta(&db).await.unwrap();
        assert_eq!(
            all.get("last_indexed_markdown").map(String::as_str),
            Some("2026-01-01T00:00:00Z")
        );
        assert_eq!(all.get("hash_markdown_a").map(String::as_str), Some("h1"));
    }

    #[tokio::test]
    async fn get_chunk_by_id_returns_none_for_unknown_id() {
        let (_dir, _conn, db) = test_conn_without_vec_and_sea_orm().await;
        assert_eq!(get_chunk_by_id(&db, 999).await.unwrap(), None);
    }

    #[tokio::test]
    async fn search_similar_ranks_by_ascending_distance() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        // Three orthogonal unit vectors.
        bulk_index_chunks(
            &conn,
            "markdown",
            "x",
            &[one_chunk("x-axis", Some(&[1.0, 0.0, 0.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        bulk_index_chunks(
            &conn,
            "markdown",
            "y",
            &[one_chunk("y-axis", Some(&[0.0, 1.0, 0.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        bulk_index_chunks(
            &conn,
            "markdown",
            "z",
            &[one_chunk("z-axis", Some(&[0.0, 0.0, 1.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let results = search_similar(&db, &[1.0, 0.0, 0.0], 5, None)
            .await
            .unwrap();
        assert_eq!(results.len(), 3);
        assert_eq!(
            results[0].chunk.source_ref, "x",
            "the exact-match vector must rank first"
        );
        assert_eq!(results[0].distance, 0.0);
        assert!(results[0].distance < results[1].distance);
        assert!(results[1].distance <= results[2].distance);
    }

    #[tokio::test]
    async fn search_similar_limit_caps_results() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        for (i, v) in [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
            .iter()
            .enumerate()
        {
            bulk_index_chunks(
                &conn,
                "markdown",
                &i.to_string(),
                &[one_chunk("c", Some(v))],
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }

        let results = search_similar(&db, &[1.0, 0.0, 0.0], 1, None)
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
    }

    #[tokio::test]
    async fn search_similar_source_type_filter_is_honored() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        bulk_index_chunks(
            &conn,
            "markdown",
            "a",
            &[one_chunk("md", Some(&[1.0, 0.0, 0.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        bulk_index_chunks(
            &conn,
            "code",
            "b",
            &[one_chunk("code", Some(&[1.0, 0.0, 0.0]))],
            "2026-01-01T00:00:00Z",
        )
        .unwrap();

        let results = search_similar(&db, &[1.0, 0.0, 0.0], 5, Some("code"))
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].chunk.source_type, "code");
    }

    #[tokio::test]
    async fn search_similar_hydrates_metadata_json() {
        let (_dir, conn, db) = test_conn_with_vec_and_sea_orm(3).await;
        let metadata = serde_json::json!({"title": "hello"});
        let chunk = NewChunk {
            chunk_text: "content",
            metadata: Some(&metadata),
            embedding: Some(&[1.0, 0.0, 0.0]),
        };
        bulk_index_chunks(&conn, "markdown", "a", &[chunk], "2026-01-01T00:00:00Z").unwrap();

        let results = search_similar(&db, &[1.0, 0.0, 0.0], 5, None)
            .await
            .unwrap();
        assert_eq!(results[0].chunk.metadata, Some(metadata));
    }

    #[tokio::test]
    async fn search_similar_returns_empty_not_an_error_without_embeddings_table() {
        let (_dir, _conn, db) = test_conn_without_vec_and_sea_orm().await;
        assert_eq!(
            search_similar(&db, &[1.0, 0.0, 0.0], 5, None)
                .await
                .unwrap(),
            Vec::new()
        );
    }

    #[tokio::test]
    async fn fetch_recent_context_filters_by_time_window_descending() {
        let (_dir, _conn, sea_orm_db) = test_conn_without_vec_and_sea_orm().await;
        crate::project_context_repository::upsert(
            &sea_orm_db,
            "old",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .await
        .unwrap();
        crate::project_context_repository::upsert(
            &sea_orm_db,
            "mid",
            "v",
            None,
            true,
            "alice",
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();
        crate::project_context_repository::upsert(
            &sea_orm_db,
            "new",
            "v",
            None,
            true,
            "alice",
            "2026-01-03T00:00:00Z",
        )
        .await
        .unwrap();

        let rows = fetch_recent_context(&sea_orm_db, "2026-01-01T12:00:00Z", None)
            .await
            .unwrap();
        let keys: Vec<&str> = rows.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(
            keys,
            vec!["new", "mid"],
            "descending, and 'old' must be excluded by the time window"
        );
    }

    #[tokio::test]
    async fn fetch_recent_context_limit_none_means_unbounded() {
        let (_dir, _conn, sea_orm_db) = test_conn_without_vec_and_sea_orm().await;
        for i in 0..10 {
            crate::project_context_repository::upsert(
                &sea_orm_db,
                &format!("k{i}"),
                "v",
                None,
                true,
                "alice",
                &format!("2026-01-01T00:00:{i:02}Z"),
            )
            .await
            .unwrap();
        }

        let rows = fetch_recent_context(&sea_orm_db, "2025-12-31T23:59:59Z", None)
            .await
            .unwrap();
        assert_eq!(rows.len(), 10);

        let limited = fetch_recent_context(&sea_orm_db, "2025-12-31T23:59:59Z", Some(3))
            .await
            .unwrap();
        assert_eq!(limited.len(), 3);
    }
}
