//! sea-orm `Entity` for `project_context` — the per-project agent DB
//! table `project_context_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, serde::Serialize)]
#[sea_orm(table_name = "project_context")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub context_key: String,
    pub value: String,
    pub description: Option<String>,
    pub created_at: Option<String>,
    pub created_by: Option<String>,
    pub updated_at: String,
    pub updated_by: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
