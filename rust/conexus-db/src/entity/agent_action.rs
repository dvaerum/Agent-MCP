//! sea-orm `Entity` for `agent_actions` — the audit-log table
//! `agent_action_repository.rs` owns. Column shapes ported verbatim
//! from `schema.rs`'s `init_schema` (the per-project agent DB).

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
    /// constraint. `agent_action_repository::list_recent` parses it
    /// into `serde_json::Value` at the repository boundary.
    pub details: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
