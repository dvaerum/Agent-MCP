//! sea-orm `Entity` for `file_metadata`. `file_metadata_repository.rs`
//! now IS the sea-orm-backed implementation for this table (Phase G) --
//! this Entity's original PR1-era round-trip test (proving it agreed
//! with the THEN-separate rusqlite repository) is superseded by that
//! repository's own tests, which exercise this same `Entity` through
//! the real, converted production code path.

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
