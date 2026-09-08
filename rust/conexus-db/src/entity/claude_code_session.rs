//! sea-orm `Entity` for `claude_code_sessions` — the per-project agent
//! DB table `claude_code_session_repository.rs` currently owns.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "claude_code_sessions")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub session_id: String,
    pub pid: i64,
    pub parent_pid: i64,
    pub first_detected: String,
    pub last_activity: String,
    pub working_directory: Option<String>,
    pub agent_id: Option<String>,
    pub status: Option<String>,
    pub git_commits: Option<String>,
    pub metadata: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
