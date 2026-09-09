//! Phase G: sea-orm `Entity`/`Model`/`ActiveModel` type definitions.
//!
//! Scope of this module, per the plan's own "Phase G" first-slice PR
//! breakdown (see `/home/dennis/.claude/plans/prancy-napping-pie.md`):
//! Entities only — a real repository still does its real work through
//! the matching hand-rolled `rusqlite`-based repository module in
//! `conexus-db::<name>_repository` until that repository is REWRITTEN
//! (PR 2+) to use the `Entity`/`Model` defined here instead. Adding an
//! `Entity` in this module changes nothing about production behavior
//! by itself — it's a type definition, proven correct against the
//! real schema by this module's own round-trip tests, not yet a new
//! code path any caller exercises.
//!
//! Each submodule owns exactly one table's `Entity`, matching this
//! crate's existing one-repository-per-table convention (`sql_util`/
//! `pagination_cache` stay the only cross-table infra modules). A
//! table gets its `Entity` added in the SAME PR its repository is
//! rewritten, except this first slice (PR 1), which defines 3 Entities
//! ahead of their own repository rewrites (PR 2/PR 3) specifically to
//! settle the module's own shape/conventions on the smallest possible
//! tables before repeating it 11 more times.

pub mod agent_action;
pub mod claude_code_session;
pub mod file_metadata;
pub mod group_capability;
pub mod group_membership;
pub mod groups;
pub mod pending_directive;
pub mod project_context;
pub mod project_membership;
pub mod scheduled_directive;
pub mod sessions;
pub mod task;
pub mod task_comment;
pub mod users;
