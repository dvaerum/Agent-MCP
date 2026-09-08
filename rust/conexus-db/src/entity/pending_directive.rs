//! sea-orm `Entity` for `pending_directive` — the per-project agent DB
//! table `pending_directive_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "pending_directive")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub poke_id: String,
    pub agent_id: String,
    pub prompt: String,
    pub priority: String,
    pub created_at: String,
    pub created_by: Option<String>,
    pub delivered_at: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
