//! sea-orm `Entity` for `file_metadata` — the per-project agent DB
//! table `file_metadata_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "file_metadata")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub filepath: String,
    pub metadata: String,
    pub last_updated: String,
    pub updated_by: String,
    pub content_hash: Option<String>,
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
        let path = dir.path().join("file_metadata_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_schema(&conn).unwrap();
            crate::file_metadata_repository::upsert(
                &conn,
                "/repo/src/main.rs",
                r#"{"lines":42}"#,
                "alice",
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].filepath, "/repo/src/main.rs");
        assert_eq!(rows[0].updated_by, "alice");
        assert_eq!(rows[0].content_hash, None);

        // `content_hash` (nullable, absent from the repository's own
        // `upsert` column list -- confirmed by reading it directly)
        // round-trips as a real Some(...) through the Entity too.
        let am = ActiveModel {
            filepath: Set("/repo/src/lib.rs".to_string()),
            metadata: Set(r#"{"lines":7}"#.to_string()),
            last_updated: Set("2026-01-01T00:01:00Z".to_string()),
            updated_by: Set("bob".to_string()),
            content_hash: Set(Some("deadbeef".to_string())),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let row = crate::file_metadata_repository::get(&conn, "/repo/src/lib.rs")
            .unwrap()
            .unwrap();
        assert_eq!(row.content_hash.as_deref(), Some("deadbeef"));
    }
}
