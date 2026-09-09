//! sea-orm `Entity` for `rag_chunks` — the per-project agent DB table
//! `rag_repository.rs` currently owns.
//!
//! `rag_embeddings` (the sqlite-vec `vec0` virtual table joined to this
//! one via `chunk_id == rowid`) has NO Entity of its own — sea-query's
//! builder has no vocabulary for `vec0`'s `MATCH`/`k =` KNN syntax, so
//! `rag_repository::search_similar`/`bulk_index_chunks` reach it via
//! `sea_orm::Statement::from_sql_and_values` (the same raw-SQL escape
//! hatch `message_repository::fetch_thread`'s `WITH RECURSIVE` CTE
//! already established under sea-orm) rather than through an Entity.
//!
//! `metadata` stays a raw `Option<String>` JSON-in-TEXT column here,
//! matching `task::Model`'s own precedent — `RagChunkRow` keeps owning
//! the lenient parse-or-None step into `serde_json::Value`.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "rag_chunks")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = true)]
    pub chunk_id: i64,
    pub source_type: String,
    pub source_ref: String,
    pub chunk_text: String,
    pub indexed_at: String,
    /// Raw JSON text — see module doc.
    pub metadata: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    #[tokio::test]
    async fn entity_round_trips_against_the_real_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rag_chunk_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO rag_chunks (source_type, source_ref, chunk_text, indexed_at, metadata) \
                 VALUES ('context', 'k1', 'some text', '2026-01-01T00:00:00Z', '{\"a\":1}')",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].source_type, "context");
        assert_eq!(rows[0].source_ref, "k1");
        assert_eq!(rows[0].chunk_text, "some text");
        assert_eq!(rows[0].metadata.as_deref(), Some("{\"a\":1}"));

        let am = ActiveModel {
            chunk_id: sea_orm::ActiveValue::NotSet,
            source_type: Set("task".to_string()),
            source_ref: Set("t1".to_string()),
            chunk_text: Set("another chunk".to_string()),
            indexed_at: Set("2026-01-02T00:00:00Z".to_string()),
            metadata: Set(None),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let (source_ref, metadata): (String, Option<String>) = conn
            .query_row(
                "SELECT source_ref, metadata FROM rag_chunks WHERE source_type = 'task'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(source_ref, "t1");
        assert!(metadata.is_none());
    }
}
