//! sea-orm `Entity` for `agents` — the per-project agent DB table
//! `agent_repository.rs` owns. Column shapes ported verbatim from
//! `schema.rs`'s `init_schema`.
//!
//! `auto_event_loop` is `INTEGER NOT NULL DEFAULT 1` in SQL, mapped to
//! `bool` here — the same INTEGER-as-bool mapping `users::Model::
//! is_sysadmin`/`scheduled_directive::Model::enabled` already use for
//! their own boolean-as-int columns; sea-orm's sqlite backend handles
//! the conversion, no manual `i32`-then-convert dance needed.
//!
//! `agent_role` carries a `CHECK (agent_role IN ('worker', 'manager'))`
//! constraint at the DDL level that sea-orm's derive macro can't
//! express (and doesn't enforce) — modeled as a plain `String` here,
//! matching `agent_repository::AgentRow::agent_role`'s own type.
//! [`agent_repository::AgentRepository::query`](crate::agent_repository::AgentRepository::query),
//! the only sea-orm-backed reader of this Entity so far, only ever
//! READS this column (filters/sorts never touch `agent_role` at all);
//! a real enum is deferred until a write path through this Entity
//! actually needs one to enforce.
//!
//! No `Relation` variants defined yet — nothing under sea-orm joins
//! against `agents` yet (`AgentRepository::query` is the only
//! sea-orm-backed reader, and it's a single-table query).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "agents")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub token: String,
    #[sea_orm(unique)]
    pub agent_id: String,
    pub created_at: String,
    pub status: String,
    pub current_task: Option<String>,
    pub working_directory: String,
    pub color: Option<String>,
    pub terminated_at: Option<String>,
    pub updated_at: Option<String>,
    pub aoe_session_id: Option<String>,
    pub auto_event_loop: bool,
    pub last_event_seen_at: Option<String>,
    pub last_activity_at: Option<String>,
    pub agent_role: String,
    pub profile: Option<String>,
    pub profile_updated_at: Option<String>,
    pub profile_reviewed_at: Option<String>,
    pub profile_updated_by: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_repository::{AgentRepository, NewAgent};
    use crate::schema::init_schema;
    use sea_orm::{ActiveValue::Set, Database, EntityTrait};

    /// Writes through the REAL rusqlite `AgentRepository::create`, reads
    /// back through the new sea-orm `Entity` (confirms the two schema
    /// shapes genuinely agree, not just "the derive macro compiled");
    /// then the reverse -- writes via `ActiveModel`, reads back through
    /// the real rusqlite `AgentRepository::get_by_id`. Both connections
    /// point at the SAME real temp-file DB (an in-memory `:memory:` DB
    /// can't be shared across two separate connection handles).
    #[tokio::test]
    async fn entity_round_trips_against_the_real_schema_and_the_real_repository() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent_entity_test.db");

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            init_schema(&conn).unwrap();
            AgentRepository::create(
                &conn,
                NewAgent {
                    token: "tok-alice",
                    agent_id: "alice",
                    created_at: "2026-01-01T00:00:00Z",
                    status: "active",
                    current_task: None,
                    working_directory: "/tmp/alice",
                    color: Some("#FF5733"),
                    agent_role: "manager",
                },
            )
            .unwrap();
        }

        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();

        let rows = Entity::find().all(&db).await.unwrap();
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(row.token, "tok-alice");
        assert_eq!(row.agent_id, "alice");
        assert_eq!(row.status, "active");
        assert_eq!(row.working_directory, "/tmp/alice");
        assert_eq!(row.color.as_deref(), Some("#FF5733"));
        assert_eq!(row.agent_role, "manager");
        assert!(
            row.auto_event_loop,
            "DB default (INTEGER DEFAULT 1) must map to bool true"
        );
        assert!(row.current_task.is_none());
        assert!(row.terminated_at.is_none());
        assert!(row.updated_at.is_none());
        assert!(row.profile.is_none());

        let am = ActiveModel {
            token: Set("tok-bob".to_string()),
            agent_id: Set("bob".to_string()),
            created_at: Set("2026-01-02T00:00:00Z".to_string()),
            status: Set("created".to_string()),
            current_task: Set(None),
            working_directory: Set("/tmp/bob".to_string()),
            color: Set(None),
            terminated_at: Set(None),
            updated_at: Set(None),
            aoe_session_id: Set(None),
            auto_event_loop: Set(false),
            last_event_seen_at: Set(None),
            last_activity_at: Set(None),
            agent_role: Set("worker".to_string()),
            profile: Set(None),
            profile_updated_at: Set(None),
            profile_reviewed_at: Set(None),
            profile_updated_by: Set(None),
        };
        am.insert(&db).await.unwrap();
        drop(db);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let bob = AgentRepository::get_by_id(&conn, "bob").unwrap().unwrap();
        assert_eq!(bob.token, "tok-bob");
        assert_eq!(bob.status, "created");
        assert_eq!(bob.working_directory, "/tmp/bob");
        assert_eq!(bob.agent_role, "worker");
        assert!(
            !bob.auto_event_loop,
            "ActiveModel-written false must round-trip as SQLite 0, read back as bool false"
        );
    }
}
