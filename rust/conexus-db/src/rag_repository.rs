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

use rusqlite::{Connection, OptionalExtension, Result, Row};
use std::collections::HashMap;

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

fn embeddings_table_exists(conn: &Connection) -> Result<bool> {
    conn.query_row(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual') AND name = 'rag_embeddings'",
        [],
        |_| Ok(()),
    )
    .optional()
    .map(|found| found.is_some())
}

fn row_to_chunk(row: &Row) -> rusqlite::Result<RagChunkRow> {
    let metadata_raw: Option<String> = row.get(5)?;
    Ok(RagChunkRow {
        chunk_id: row.get(0)?,
        source_type: row.get(1)?,
        source_ref: row.get(2)?,
        chunk_text: row.get(3)?,
        indexed_at: row.get(4)?,
        metadata: metadata_raw.and_then(|s| serde_json::from_str(&s).ok()),
    })
}

const CHUNK_COLUMNS: &str = "chunk_id, source_type, source_ref, chunk_text, indexed_at, metadata";

/// Insert N chunks + their companion embeddings. Returns the count of
/// chunk rows actually written (empty-text chunks are skipped and
/// don't count). An embedding is written only when both the chunk
/// provides one AND `rag_embeddings` exists — otherwise the chunk row
/// still lands, its embedding silently skipped (the degrade path: a
/// host without sqlite-vec still gets full-text-searchable rows, just
/// not vector-searchable ones).
pub fn bulk_index_chunks(
    conn: &Connection,
    source_type: &str,
    source_ref: &str,
    chunks: &[NewChunk],
    now_iso: &str,
) -> Result<i64> {
    let has_embeddings_table = embeddings_table_exists(conn)?;
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
pub fn delete_chunks_for(conn: &Connection, source_type: &str, source_ref: &str) -> Result<i64> {
    if embeddings_table_exists(conn)? {
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
pub fn purge_source(conn: &Connection, source_type: &str, source_ref: &str) -> Result<i64> {
    if embeddings_table_exists(conn)? {
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

pub fn get_last_indexed(conn: &Connection, source_type: &str) -> Result<Option<String>> {
    let key = format!("last_indexed_{source_type}");
    conn.query_row(
        "SELECT meta_value FROM rag_meta WHERE meta_key = ?1",
        [&key],
        |row| row.get(0),
    )
    .optional()
}

/// Bulk read of every `rag_meta` row — the indexer's per-cycle
/// prelude. Rows with a `NULL` value are omitted (a `HashMap<String,
/// String>` has no way to represent one).
pub fn get_all_meta(conn: &Connection) -> Result<HashMap<String, String>> {
    let mut stmt = conn.prepare("SELECT meta_key, meta_value FROM rag_meta")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let (key, value) = row?;
        if let Some(value) = value {
            map.insert(key, value);
        }
    }
    Ok(map)
}

pub fn get_chunk_by_id(conn: &Connection, chunk_id: i64) -> Result<Option<RagChunkRow>> {
    conn.query_row(
        &format!("SELECT {CHUNK_COLUMNS} FROM rag_chunks WHERE chunk_id = ?1"),
        [chunk_id],
        row_to_chunk,
    )
    .optional()
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
pub fn search_similar(
    conn: &Connection,
    query_embedding: &[f32],
    limit: i64,
    source_type_filter: Option<&str>,
) -> Result<Vec<RagSearchResult>> {
    if !embeddings_table_exists(conn)? {
        return Ok(Vec::new());
    }

    let effective_k = if source_type_filter.is_some() {
        limit * 4
    } else {
        limit
    };
    let query_json = serde_json::to_string(query_embedding).expect("a &[f32] always serializes");

    let mut stmt = conn.prepare(&format!(
        "SELECT {}, r.distance FROM rag_embeddings r \
         JOIN rag_chunks c ON r.rowid = c.chunk_id \
         WHERE r.embedding MATCH ?1 AND k = ?2 ORDER BY r.distance",
        CHUNK_COLUMNS
            .split(", ")
            .map(|c| format!("c.{c}"))
            .collect::<Vec<_>>()
            .join(", ")
    ))?;

    let rows = stmt.query_map((&query_json, effective_k), |row| {
        let metadata_raw: Option<String> = row.get(5)?;
        Ok((
            RagChunkRow {
                chunk_id: row.get(0)?,
                source_type: row.get(1)?,
                source_ref: row.get(2)?,
                chunk_text: row.get(3)?,
                indexed_at: row.get(4)?,
                metadata: metadata_raw.and_then(|s| serde_json::from_str(&s).ok()),
            },
            row.get::<_, f64>(6)?,
        ))
    })?;

    let mut results = Vec::new();
    for row in rows {
        let (chunk, distance) = row?;
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
/// Python call shape.
pub fn fetch_recent_context(
    conn: &Connection,
    since: &str,
    limit: Option<i64>,
) -> Result<Vec<RecentContextEntry>> {
    fn map_row(row: &Row) -> rusqlite::Result<RecentContextEntry> {
        Ok(RecentContextEntry {
            context_key: row.get(0)?,
            value: row.get(1)?,
            description: row.get(2)?,
            updated_at: row.get(3)?,
        })
    }

    const BASE_SQL: &str =
        "SELECT context_key, value, description, updated_at FROM project_context \
         WHERE updated_at > ?1 ORDER BY updated_at DESC";

    match limit {
        Some(l) => {
            let mut stmt = conn.prepare(&format!("{BASE_SQL} LIMIT ?2"))?;
            let rows = stmt.query_map((since, l), map_row)?.collect();
            rows
        }
        None => {
            let mut stmt = conn.prepare(BASE_SQL)?;
            let rows = stmt.query_map([since], map_row)?.collect();
            rows
        }
    }
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

    fn test_conn_without_vec() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn one_chunk<'a>(text: &'a str, embedding: Option<&'a [f32]>) -> NewChunk<'a> {
        NewChunk {
            chunk_text: text,
            metadata: None,
            embedding,
        }
    }

    #[test]
    fn bulk_index_chunks_writes_chunk_and_embedding_rows() {
        let conn = test_conn_with_vec(3);
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

        let chunk = get_chunk_by_id(&conn, 1).unwrap().unwrap();
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

    #[test]
    fn bulk_index_chunks_writes_chunk_row_even_without_an_embedding() {
        let conn = test_conn_with_vec(3);
        let chunks = vec![one_chunk("no vector for this one", None)];

        let inserted =
            bulk_index_chunks(&conn, "markdown", "a", &chunks, "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(inserted, 1);
        assert!(get_chunk_by_id(&conn, 1).unwrap().is_some());

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM rag_embeddings", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            count, 0,
            "no embedding was provided, so none should have been written"
        );
    }

    #[test]
    fn bulk_index_chunks_degrades_gracefully_without_an_embeddings_table() {
        let conn = test_conn_without_vec();
        let embedding = [1.0f32, 0.0, 0.0];
        let chunks = vec![one_chunk("still indexed as text", Some(&embedding))];

        let inserted =
            bulk_index_chunks(&conn, "markdown", "a", &chunks, "2026-01-01T00:00:00Z").unwrap();
        assert_eq!(
            inserted, 1,
            "the chunk row must still land even with no rag_embeddings table"
        );
        assert!(get_chunk_by_id(&conn, 1).unwrap().is_some());
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

    #[test]
    fn set_meta_and_get_last_indexed_round_trip() {
        let conn = test_conn_without_vec();
        assert_eq!(get_last_indexed(&conn, "markdown").unwrap(), None);

        set_meta(&conn, "markdown", Some("2026-01-01T00:00:00Z"), None).unwrap();
        assert_eq!(
            get_last_indexed(&conn, "markdown").unwrap().as_deref(),
            Some("2026-01-01T00:00:00Z")
        );

        // Re-writing (INSERT OR REPLACE) must overwrite, not conflict.
        set_meta(&conn, "markdown", Some("2026-01-02T00:00:00Z"), None).unwrap();
        assert_eq!(
            get_last_indexed(&conn, "markdown").unwrap().as_deref(),
            Some("2026-01-02T00:00:00Z")
        );
    }

    #[test]
    fn set_meta_writes_source_hashes_independently_of_last_indexed_at() {
        let conn = test_conn_without_vec();
        set_meta(
            &conn,
            "context",
            None,
            Some(&[("key-a", "hash-a"), ("key-b", "hash-b")]),
        )
        .unwrap();

        assert_eq!(
            get_last_indexed(&conn, "context").unwrap(),
            None,
            "no last_indexed_at was given, so it must stay unwritten"
        );
        let all = get_all_meta(&conn).unwrap();
        assert_eq!(
            all.get("hash_context_key-a").map(String::as_str),
            Some("hash-a")
        );
        assert_eq!(
            all.get("hash_context_key-b").map(String::as_str),
            Some("hash-b")
        );
    }

    #[test]
    fn get_all_meta_returns_every_row() {
        let conn = test_conn_without_vec();
        set_meta(
            &conn,
            "markdown",
            Some("2026-01-01T00:00:00Z"),
            Some(&[("a", "h1")]),
        )
        .unwrap();

        let all = get_all_meta(&conn).unwrap();
        assert_eq!(
            all.get("last_indexed_markdown").map(String::as_str),
            Some("2026-01-01T00:00:00Z")
        );
        assert_eq!(all.get("hash_markdown_a").map(String::as_str), Some("h1"));
    }

    #[test]
    fn get_chunk_by_id_returns_none_for_unknown_id() {
        let conn = test_conn_without_vec();
        assert_eq!(get_chunk_by_id(&conn, 999).unwrap(), None);
    }

    #[test]
    fn search_similar_ranks_by_ascending_distance() {
        let conn = test_conn_with_vec(3);
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

        let results = search_similar(&conn, &[1.0, 0.0, 0.0], 5, None).unwrap();
        assert_eq!(results.len(), 3);
        assert_eq!(
            results[0].chunk.source_ref, "x",
            "the exact-match vector must rank first"
        );
        assert_eq!(results[0].distance, 0.0);
        assert!(results[0].distance < results[1].distance);
        assert!(results[1].distance <= results[2].distance);
    }

    #[test]
    fn search_similar_limit_caps_results() {
        let conn = test_conn_with_vec(3);
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

        let results = search_similar(&conn, &[1.0, 0.0, 0.0], 1, None).unwrap();
        assert_eq!(results.len(), 1);
    }

    #[test]
    fn search_similar_source_type_filter_is_honored() {
        let conn = test_conn_with_vec(3);
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

        let results = search_similar(&conn, &[1.0, 0.0, 0.0], 5, Some("code")).unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].chunk.source_type, "code");
    }

    #[test]
    fn search_similar_hydrates_metadata_json() {
        let conn = test_conn_with_vec(3);
        let metadata = serde_json::json!({"title": "hello"});
        let chunk = NewChunk {
            chunk_text: "content",
            metadata: Some(&metadata),
            embedding: Some(&[1.0, 0.0, 0.0]),
        };
        bulk_index_chunks(&conn, "markdown", "a", &[chunk], "2026-01-01T00:00:00Z").unwrap();

        let results = search_similar(&conn, &[1.0, 0.0, 0.0], 5, None).unwrap();
        assert_eq!(results[0].chunk.metadata, Some(metadata));
    }

    #[test]
    fn search_similar_returns_empty_not_an_error_without_embeddings_table() {
        let conn = test_conn_without_vec();
        assert_eq!(
            search_similar(&conn, &[1.0, 0.0, 0.0], 5, None).unwrap(),
            Vec::new()
        );
    }

    #[test]
    fn fetch_recent_context_filters_by_time_window_descending() {
        let conn = test_conn_without_vec();
        crate::project_context_repository::upsert(
            &conn,
            "old",
            "v",
            None,
            true,
            "alice",
            "2026-01-01T00:00:00Z",
        )
        .unwrap();
        crate::project_context_repository::upsert(
            &conn,
            "mid",
            "v",
            None,
            true,
            "alice",
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
        crate::project_context_repository::upsert(
            &conn,
            "new",
            "v",
            None,
            true,
            "alice",
            "2026-01-03T00:00:00Z",
        )
        .unwrap();

        let rows = fetch_recent_context(&conn, "2026-01-01T12:00:00Z", None).unwrap();
        let keys: Vec<&str> = rows.iter().map(|r| r.context_key.as_str()).collect();
        assert_eq!(
            keys,
            vec!["new", "mid"],
            "descending, and 'old' must be excluded by the time window"
        );
    }

    #[test]
    fn fetch_recent_context_limit_none_means_unbounded() {
        let conn = test_conn_without_vec();
        for i in 0..10 {
            crate::project_context_repository::upsert(
                &conn,
                &format!("k{i}"),
                "v",
                None,
                true,
                "alice",
                &format!("2026-01-01T00:00:{i:02}Z"),
            )
            .unwrap();
        }

        let rows = fetch_recent_context(&conn, "2025-12-31T23:59:59Z", None).unwrap();
        assert_eq!(rows.len(), 10);

        let limited = fetch_recent_context(&conn, "2025-12-31T23:59:59Z", Some(3)).unwrap();
        assert_eq!(limited.len(), 3);
    }
}
