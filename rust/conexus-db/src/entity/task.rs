//! sea-orm `Entity` for `tasks` — the per-project agent DB table
//! `task_repository.rs` currently owns.
//!
//! Unlike every other Entity in this module, `Model` here is NOT
//! aliased directly to `task_repository::TaskRow` — `child_tasks`/
//! `depends_on_tasks`/`notes` are stored as JSON-in-TEXT columns that
//! `TaskRow` parses leniently (malformed JSON or a NULL column
//! degrades to `None`, matching `rag_repository::RagChunkRow::metadata`'s
//! established convention). `Model` represents the column exactly as
//! SQLite stores it (`Option<String>`, raw JSON text); `task_repository`
//! itself owns the `Model` -> `TaskRow` parsing step, the same
//! responsibility split it already has today between its own
//! `row_to_task` row-mapper and the public `TaskRow` type.
//!
//! Two invariants live at the DDL level, not in this Entity:
//! `idx_tasks_single_root` (a partial/expression unique index enforcing
//! R15-BL-1's single-root-task rule) and `trg_tasks_terminal_state_guard`
//! (a `BEFORE UPDATE` trigger raising `terminal_task_guard: ...` —
//! `GUARD_MARKER` in `task_repository.rs` substring-matches this out of
//! a propagated `DbErr`, the identical pattern already proven under
//! sea-orm by `task_comments_repository.rs`, PR #970). Neither an
//! Entity nor a `Relation` can express either — both stay purely a
//! `schema.rs`-DDL and call-site-error-classification concern.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "tasks")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub task_id: String,
    pub title: String,
    pub description: Option<String>,
    pub assigned_to: Option<String>,
    pub created_by: String,
    pub status: String,
    pub priority: String,
    pub created_at: String,
    pub updated_at: String,
    pub parent_task: Option<String>,
    /// Raw JSON text (`Vec<String>` serialized) — see module doc.
    pub child_tasks: Option<String>,
    /// Raw JSON text (`Vec<String>` serialized) — see module doc.
    pub depends_on_tasks: Option<String>,
    /// Raw JSON text (`Vec<TaskNote>` serialized) — see module doc.
    pub notes: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
