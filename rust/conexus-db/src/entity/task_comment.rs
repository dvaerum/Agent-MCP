//! sea-orm `Entity` for `task_comments` — the per-project agent DB
//! table `task_comments_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "task_comments")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub note_id: i64,
    pub task_id: String,
    pub author: Option<String>,
    pub timestamp: String,
    pub text: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
