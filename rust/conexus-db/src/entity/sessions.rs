//! sea-orm `Entity` for `sessions` — lives on the ROUTER db
//! (`schema::init_router_schema`), same distinction `group_capability`
//! already establishes for the same DB file.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "sessions")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub session_id: String,
    pub user_id: String,
    pub created_at: String,
    pub expires_at: String,
    pub last_used_at: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {
    #[sea_orm(
        belongs_to = "super::users::Entity",
        from = "Column::UserId",
        to = "super::users::Column::UserId",
        on_delete = "Cascade"
    )]
    User,
}

impl Related<super::users::Entity> for Entity {
    fn to() -> RelationDef {
        Relation::User.def()
    }
}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_router_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    #[tokio::test]
    async fn entity_round_trips_against_the_real_router_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sessions_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_router_schema(&conn).unwrap();
            conn.execute(
                "INSERT INTO users (user_id, username, created_at, is_sysadmin) \
                 VALUES ('u1', 'alice', '2026-01-01T00:00:00Z', 0)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_used_at) \
                 VALUES ('s1', 'u1', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', \
                 '2026-01-01T00:00:00Z')",
                [],
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].session_id, "s1");
        assert_eq!(rows[0].user_id, "u1");
        assert_eq!(rows[0].expires_at, "2026-01-02T00:00:00Z");

        let am = ActiveModel {
            session_id: Set("s2".to_string()),
            user_id: Set("u1".to_string()),
            created_at: Set("2026-01-03T00:00:00Z".to_string()),
            expires_at: Set("2026-01-04T00:00:00Z".to_string()),
            last_used_at: Set("2026-01-03T00:00:00Z".to_string()),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let user_id: String = conn
            .query_row(
                "SELECT user_id FROM sessions WHERE session_id = 's2'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(user_id, "u1");
    }
}
