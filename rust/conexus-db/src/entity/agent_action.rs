//! sea-orm `Entity` for `agent_actions` — the audit-log table
//! `agent_action_repository.rs` currently owns via hand-rolled
//! `rusqlite`. Column shapes ported verbatim from `schema.rs`'s
//! `init_schema` (the per-project agent DB).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "agent_actions")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub action_id: i64,
    pub agent_id: String,
    pub action_type: String,
    pub task_id: Option<String>,
    pub timestamp: String,
    /// Stored as its JSON-serialized TEXT (`agent_action_repository::
    /// log_agent_action`'s own convention) — kept as `String` here,
    /// not sea-orm's `Json` column type, since the real column is
    /// declared plain `TEXT` with no `CHECK (json_valid(...))`
    /// constraint; a future repository rewrite (PR 2) decides whether
    /// to parse this eagerly.
    pub details: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    /// Round-trip proof: a row written through the REAL repository
    /// (`rusqlite`, the production path today) is readable through
    /// this `Entity`, and a row written through this `Entity` is
    /// readable back through it too — the schema shapes genuinely
    /// agree, not just "the derive macro compiled."
    #[tokio::test]
    async fn entity_round_trips_against_the_real_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent_action_entity_test.db");

        // Schema + one row via the real, current rusqlite path.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_schema(&conn).unwrap();
            crate::agent_action_repository::log_agent_action(
                &conn,
                "alice",
                "registered_agent",
                None,
                None,
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].agent_id, "alice");
        assert_eq!(rows[0].action_type, "registered_agent");
        assert_eq!(rows[0].task_id, None);
        assert_eq!(rows[0].details, None);

        // A row inserted through the Entity/ActiveModel is readable
        // back through the SAME rusqlite repository the production
        // path still uses today.
        let am = ActiveModel {
            agent_id: Set("bob".to_string()),
            action_type: Set("terminated_agent".to_string()),
            task_id: Set(Some("t-1".to_string())),
            timestamp: Set("2026-01-01T00:01:00Z".to_string()),
            details: Set(Some(r#"{"reason":"idle"}"#.to_string())),
            ..Default::default()
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let recent = crate::agent_action_repository::list_recent(&conn, None, None, 10).unwrap();
        assert_eq!(recent.len(), 2);
        let bob_row = recent.iter().find(|r| r.agent_id == "bob").unwrap();
        assert_eq!(bob_row.action_type, "terminated_agent");
        assert_eq!(bob_row.task_id.as_deref(), Some("t-1"));
        assert_eq!(bob_row.details, Some(serde_json::json!({"reason": "idle"})));
    }
}
