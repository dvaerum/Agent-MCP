//! sea-orm `Entity` for `agent_messages` — the per-project message
//! table `message_repository.rs` owns. Column shapes ported verbatim
//! from `schema.rs`'s `init_schema`.
//!
//! `delivered`/`read` are `INTEGER NOT NULL DEFAULT 0` in SQL, mapped
//! to `bool` here — the same INTEGER-as-bool mapping `entity::agent::
//! Model::auto_event_loop` already uses for its own boolean-as-int
//! column; sea-orm's sqlite backend handles the conversion, no manual
//! `i32`-then-convert dance needed. `read` is a valid plain Rust field
//! name (not a keyword), so no `r#read` escaping is needed.
//!
//! `message_type`/`priority` carry no DB-level `CHECK` constraint
//! (unlike, say, `agent::Model::agent_role`) — they're validated
//! app-side, at the REST boundary, via `conexus_backend::rest_handlers`'s
//! own `MESSAGE_TYPES`/`MESSAGE_PRIORITIES` consts, not anywhere in
//! this crate, so both are modeled as plain `String` here with nothing
//! extra to note.
//!
//! `parent_message_id` is self-referential (`message_id` of another
//! row in this same table) but has no DB-level `FOREIGN KEY` —
//! `message_repository::send` validates existence in application code
//! instead (see that module's own PF-R32-1 doc). No `Relation` variant
//! is defined for it: nothing under sea-orm joins against `agent_messages`
//! yet (every sea-orm-backed method on this Entity so far is a
//! single-table query/write), matching `entity::agent`'s own precedent
//! of leaving `Relation` empty until a real join shows up.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "agent_messages")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub message_id: String,
    pub sender_id: String,
    pub recipient_id: String,
    pub message_content: String,
    pub message_type: String,
    pub priority: String,
    pub timestamp: String,
    pub delivered: bool,
    pub read: bool,
    pub subject: Option<String>,
    pub parent_message_id: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
