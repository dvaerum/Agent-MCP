//! sea-orm `Entity` for `project_settings` — the per-project operator-
//! only config table `project_settings_repository.rs` owns. Byte-for-
//! byte identical column shape to [`crate::entity::project_context`]
//! (see that repository's own module doc, and `project_settings_
//! repository`'s, for why the two stay separate tables/Entities
//! despite the duplication — ADR-0016's table-separation safety
//! boundary).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel, serde::Serialize)]
#[sea_orm(table_name = "project_settings")]
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
