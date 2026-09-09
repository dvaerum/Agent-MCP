//! sea-orm `Entity` for `rag_meta` — the per-project agent DB table
//! `rag_repository.rs` currently owns. A plain key/value table; no
//! `Relation`s, matching the same shape as every other zero-FK Entity
//! in this module.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "rag_meta")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub meta_key: String,
    pub meta_value: Option<String>,
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
        let path = dir.path().join("rag_meta_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO rag_meta (meta_key, meta_value) VALUES ('hash_context_k1', 'abc123')",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].meta_key, "hash_context_k1");
        assert_eq!(rows[0].meta_value.as_deref(), Some("abc123"));

        let am = ActiveModel {
            meta_key: Set("last_indexed_context".to_string()),
            meta_value: Set(Some("2026-01-01T00:00:00Z".to_string())),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let meta_value: Option<String> = conn
            .query_row(
                "SELECT meta_value FROM rag_meta WHERE meta_key = 'last_indexed_context'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(meta_value.as_deref(), Some("2026-01-01T00:00:00Z"));
    }
}
