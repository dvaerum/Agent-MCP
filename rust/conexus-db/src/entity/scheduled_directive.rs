//! sea-orm `Entity` for `scheduled_directive` — the per-project agent
//! DB table `scheduled_directive_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "scheduled_directive")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub directive_id: String,
    pub agent_id: String,
    pub prompt: String,
    pub interval_seconds: i64,
    pub next_due_at: String,
    pub enabled: bool,
    pub status: String,
    pub until_at: Option<String>,
    pub max_runs: Option<i64>,
    pub run_count: i64,
    pub created_at: String,
    pub created_by: Option<String>,
    pub updated_at: Option<String>,
    pub updated_by: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
