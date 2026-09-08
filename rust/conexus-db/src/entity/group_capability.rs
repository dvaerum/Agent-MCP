//! sea-orm `Entity` for `group_capability` — lives on the ROUTER db
//! (`schema::init_router_schema`), not the per-project agent db,
//! confirmed the same way `group_capability_repository.rs`'s own
//! module doc already confirms it (PR #774). This `Entity` is used
//! against whatever `DatabaseConnection` points at `router.db`; a
//! sea-orm `Entity` is just a table-shape definition, not bound to a
//! specific connection.
//!
//! No `Relation` to a `groups` Entity yet — `groups`/`users` have no
//! dedicated `conexus-db` repository or Entity anywhere in this
//! workspace today (a real, tracked scope gap; see the plan's own
//! Phase G research pass 2 notes). The real `FOREIGN KEY (group_id)
//! REFERENCES groups(group_id) ON DELETE CASCADE` constraint still
//! exists at the SQL level regardless of whether sea-orm models it.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "group_capability")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub group_id: String,
    #[sea_orm(primary_key, auto_increment = false)]
    pub capability: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    #[tokio::test]
    async fn entity_round_trips_against_the_real_router_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("group_capability_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO groups (group_id, name, is_sysadmin, created_at) \
                 VALUES ('g1', 'g1', 0, '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
            crate::group_capability_repository::replace(&conn, "g1", ["system.projects.manage"])
                .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].group_id, "g1");
        assert_eq!(rows[0].capability, "system.projects.manage");

        // Composite primary key round-trips through the ActiveModel
        // (a second capability row for the same group).
        let am = ActiveModel {
            group_id: Set("g1".to_string()),
            capability: Set("system.users.manage".to_string()),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let caps = crate::group_capability_repository::fetch(&conn, "g1").unwrap();
        assert_eq!(caps.len(), 2);
        assert!(caps.contains("system.projects.manage"));
        assert!(caps.contains("system.users.manage"));
    }
}
