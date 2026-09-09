//! Full port of `agent_mcp/repositories/task_repository.py`, landed
//! in 2 PRs per the migration plan's progress log (core CRUD, then
//! this — `bulk_update_fields` + the `terminal_task_guard` DB-trigger
//! detection). This is the LAST of the 8 repositories — Phase B is
//! complete once this lands.
//!
//! Python's `connection=` transaction-seam (raw cursor / `Session`
//! branches on `create`/`update_fields`/`delete`) has NO Rust
//! equivalent to port: this crate already uses a uniform
//! `&Connection`-only seam everywhere (see [`create`]'s doc) — the
//! caller composing a wider transaction just passes the SAME
//! `&Connection` it's already mid-transaction on, which is exactly
//! what Python's cursor-shaped `connection=` was standing in for.
//! There's no separate "shape" left to add.
//!
//! Unlike `AgentRepository`/`MessageRepository`, this repository has
//! NO `StableOrderCache`/pagination method at all — confirmed via
//! research: Python's task listing/filtering/pagination lives outside
//! this file entirely, in `features/task_queries.py`'s
//! `TaskQueryEngine` (an in-memory-cache-driven engine, not
//! SQL-backed). Porting `TaskQueryEngine`'s equivalent is out of
//! scope here — it isn't "porting `task_repository.py`," it's new
//! design work for a different phase.
//!
//! A module of plain functions — no cache, no wrapper type needed
//! (same rule as `project_context_repository`/`rag_repository`).
//!
//! ## Load-bearing invariants preserved from Python
//! - **Collision-resistant id minting**: [`generate_task_id`] uses a
//!   real OS CSPRNG (`getrandom`), not a timestamp — Python
//!   consolidated three previously-divergent generators specifically
//!   because two of them (`task_<millisecond-timestamp>`) could
//!   collide under concurrent same-millisecond creates, producing a
//!   duplicate-PK error. Unlike every other repository's `create()`
//!   in this crate, the caller is NOT required to supply `task_id` —
//!   see [`NewTask::task_id`]'s doc for why this is the one deliberate
//!   exception to that pattern.
//! - **`create()`/`create_in_transaction()` do NOT swallow a
//!   duplicate-id conflict** — the real `DbErr`/`rusqlite::Error`
//!   propagates uncaught, matching Python's explicit choice
//!   (documented rationale: "silently returning the existing row
//!   would mask write conflicts").
//! - **Single-root-task expression index** (`idx_tasks_single_root`,
//!   in `schema.rs`) is a SCHEMA-level invariant, not an app-level
//!   check — it must exist in the DDL, not just be re-verified in
//!   code.

use rusqlite::{Connection, OptionalExtension, Result, Row, ToSql};
use sea_orm::{
    ActiveValue::Set, ColumnTrait, DatabaseConnection, DbErr, EntityTrait, PaginatorTrait,
    QueryFilter, QueryOrder, QuerySelect,
};
use std::collections::HashMap;

use crate::entity::task::{self, Column, Entity};
use crate::scheduled_directive_repository::NullableUpdate;

/// One note entry in a task's `notes` JSON list.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct TaskNote {
    pub timestamp: String,
    pub author: Option<String>,
    pub content: String,
}

/// One row of the `tasks` table. `child_tasks`/`depends_on_tasks`/
/// `notes` are parsed from their stored JSON text; malformed JSON (or
/// a NULL column) degrades to `None` rather than an error — matching
/// this crate's established leniency for JSON-in-TEXT columns (see
/// `rag_repository::RagChunkRow::metadata`).
#[derive(Debug, Clone, PartialEq)]
pub struct TaskRow {
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
    pub child_tasks: Option<Vec<String>>,
    pub depends_on_tasks: Option<Vec<String>>,
    pub notes: Option<Vec<TaskNote>>,
}

const COLUMNS: &str = "task_id, title, description, assigned_to, created_by, status, priority, \
     created_at, updated_at, parent_task, child_tasks, depends_on_tasks, notes";

fn row_to_task(row: &Row) -> rusqlite::Result<TaskRow> {
    let child_raw: Option<String> = row.get(10)?;
    let depends_raw: Option<String> = row.get(11)?;
    let notes_raw: Option<String> = row.get(12)?;
    Ok(TaskRow {
        task_id: row.get(0)?,
        title: row.get(1)?,
        description: row.get(2)?,
        assigned_to: row.get(3)?,
        created_by: row.get(4)?,
        status: row.get(5)?,
        priority: row.get(6)?,
        created_at: row.get(7)?,
        updated_at: row.get(8)?,
        parent_task: row.get(9)?,
        child_tasks: child_raw.and_then(|s| serde_json::from_str(&s).ok()),
        depends_on_tasks: depends_raw.and_then(|s| serde_json::from_str(&s).ok()),
        notes: notes_raw.and_then(|s| serde_json::from_str(&s).ok()),
    })
}

/// `task::Model` -> `TaskRow`, applying the identical lenient
/// JSON-in-TEXT parsing [`row_to_task`] uses (malformed JSON or a NULL
/// column degrades to `None`) -- the sea-orm counterpart of that
/// row-mapper, used by [`list_all`]/[`list_by_agent`].
fn task_row_from_model(m: task::Model) -> TaskRow {
    TaskRow {
        task_id: m.task_id,
        title: m.title,
        description: m.description,
        assigned_to: m.assigned_to,
        created_by: m.created_by,
        status: m.status,
        priority: m.priority,
        created_at: m.created_at,
        updated_at: m.updated_at,
        parent_task: m.parent_task,
        child_tasks: m.child_tasks.and_then(|s| serde_json::from_str(&s).ok()),
        depends_on_tasks: m
            .depends_on_tasks
            .and_then(|s| serde_json::from_str(&s).ok()),
        notes: m.notes.and_then(|s| serde_json::from_str(&s).ok()),
    }
}

/// Mints a collision-resistant task id: `task_` + 12 hex chars from
/// the OS CSPRNG (mirrors Python's `secrets.token_hex(6)`) — NOT a
/// timestamp, which is exactly the bug class this replaced (two
/// same-millisecond concurrent creates would otherwise collide on a
/// duplicate PK).
pub fn generate_task_id() -> String {
    let mut buf = [0u8; 6];
    getrandom::fill(&mut buf).expect("OS RNG must be available");
    let hex: String = buf.iter().map(|b| format!("{b:02x}")).collect();
    format!("task_{hex}")
}

/// Single task by id, or `None`. Built via sea-orm's typed
/// `find_by_id`, same idiom as this module's other single-row reads.
pub async fn get_by_id(db: &DatabaseConnection, task_id: &str) -> Result<Option<TaskRow>, DbErr> {
    let row = Entity::find_by_id(task_id).one(db).await?;
    Ok(row.map(task_row_from_model))
}

/// Deliberately-still-sync twin of [`get_by_id`] for every caller that
/// reads inside an in-flight, uncommitted `rusqlite::Transaction`
/// shared with the caller's own writes -- same rationale as
/// [`list_all_in_transaction`]'s own doc: `sea_orm_db` is a genuinely
/// SEPARATE connection pool from the legacy rusqlite connection, so a
/// read through it here would see pre-transaction (stale) data, not
/// this transaction's own not-yet-committed rows. Not deleted when
/// every remaining caller is itself eventually converted to a sea-orm
/// transaction -- delete this helper THEN, once nothing calls it.
pub fn get_by_id_in_transaction(conn: &Connection, task_id: &str) -> Result<Option<TaskRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM tasks WHERE task_id = ?1"),
        [task_id],
        row_to_task,
    )
    .optional()
}

/// Every task, newest first, optionally capped. Built via sea-orm's
/// typed query builder (`.order_by_desc(...)` + `.limit(...)`), same
/// idiom as [`list_assigned_updated_since`].
pub async fn list_all(db: &DatabaseConnection, limit: Option<i64>) -> Result<Vec<TaskRow>, DbErr> {
    let mut query = Entity::find().order_by_desc(Column::CreatedAt);
    if let Some(l) = limit {
        query = query.limit(l as u64);
    }
    let rows = query.all(db).await?;
    Ok(rows.into_iter().map(task_row_from_model).collect())
}

/// Deliberately-still-sync twin of [`list_all`] for the ONE remaining
/// caller that cannot use the sea-orm version: `task_mutation_engine::
/// advance_dependents_after_completion`, which reads inside an
/// in-flight, uncommitted `rusqlite::Transaction` shared with the
/// caller's own writes. `sea_orm_db` is a genuinely SEPARATE connection
/// pool from the legacy rusqlite connection -- a read through it would
/// see pre-transaction (stale) data, not this transaction's own
/// not-yet-committed rows, silently breaking BL-R29-1's dependency
/// auto-advance. Matches this migration's established "keep legacy
/// alive for a not-yet-converted call in the same function body"
/// pattern (see `rag_repository::embeddings_table_exists_sync`'s own
/// precedent). Not deleted when `advance_dependents_after_completion`
/// itself is eventually converted (PR4/5+) -- delete this helper THEN,
/// once nothing calls it.
pub fn list_all_in_transaction(conn: &Connection, limit: Option<i64>) -> Result<Vec<TaskRow>> {
    match limit {
        Some(l) => {
            let mut stmt = conn.prepare(&format!(
                "SELECT {COLUMNS} FROM tasks ORDER BY created_at DESC LIMIT ?1"
            ))?;
            let rows = stmt.query_map([l], row_to_task)?.collect();
            rows
        }
        None => {
            let mut stmt = conn.prepare(&format!(
                "SELECT {COLUMNS} FROM tasks ORDER BY created_at DESC"
            ))?;
            let rows = stmt.query_map([], row_to_task)?.collect();
            rows
        }
    }
}

/// Task count per `status`. Built via sea-orm's typed aggregate query
/// (`select_only` + `column_as(.count(), ..)` + `group_by`), not a raw
/// `Statement` escape hatch -- `QuerySelect` cleanly expresses this
/// exact `GROUP BY status, COUNT(*)` shape (proven by sea-orm's own
/// `relational_tests::group_by` test), so there's nothing here a raw
/// SQL string would express more simply.
pub async fn count_by_status(db: &DatabaseConnection) -> Result<HashMap<String, i64>, DbErr> {
    let counts: Vec<(String, i64)> = Entity::find()
        .select_only()
        .column(Column::Status)
        .column_as(Column::TaskId.count(), "count")
        .group_by(Column::Status)
        .into_tuple()
        .all(db)
        .await?;
    Ok(counts.into_iter().collect())
}

/// Tasks for one agent, newest first, with an optional status filter
/// and an optional cap. Built via sea-orm's typed query builder, same
/// idiom as [`list_all`].
pub async fn list_by_agent(
    db: &DatabaseConnection,
    agent_id: &str,
    status_filter: Option<&str>,
    limit: Option<i64>,
) -> Result<Vec<TaskRow>, DbErr> {
    let mut query = Entity::find()
        .filter(Column::AssignedTo.eq(agent_id))
        .order_by_desc(Column::CreatedAt);
    if let Some(s) = status_filter {
        query = query.filter(Column::Status.eq(s));
    }
    if let Some(l) = limit {
        query = query.limit(l as u64);
    }
    let rows = query.all(db).await?;
    Ok(rows.into_iter().map(task_row_from_model).collect())
}

/// Skinny row for the `wait_for_events`/`fetch_events_since` task-event
/// streams (`conexus-wakeloop::event_feed`) -- a "pointer, not a dump":
/// enough to build a `task_assigned`/`task_changed`/
/// `unassigned_task_appeared` event without the full [`TaskRow`]'s
/// notes/relations, matching the skinny projection Python's own
/// collectors build inline from raw cursor rows.
#[derive(Debug, Clone, PartialEq)]
pub struct TaskEventRow {
    pub task_id: String,
    pub title: String,
    pub status: String,
    pub priority: String,
    pub created_at: String,
    pub updated_at: String,
}

fn task_event_from_model(m: task::Model) -> TaskEventRow {
    TaskEventRow {
        task_id: m.task_id,
        title: m.title,
        status: m.status,
        priority: m.priority,
        created_at: m.created_at,
        updated_at: m.updated_at,
    }
}

/// Tasks assigned to `agent_id` touched since `since` (`updated_at >
/// since`), oldest first. Feeds `_collect_events_with_cap`'s
/// `task_assigned`/`task_changed` stream -- the caller classifies each
/// row using `created_at` vs `since` (v1 heuristic: `created_at >
/// since` is a fresh assignment, else a mutation of an existing one).
/// Built via sea-orm's typed query builder (`.filter(...).order_by_asc(...)`),
/// not a raw `Statement` -- a plain `WHERE ... ORDER BY` this crate's
/// established `count_active_by_assignee`-style idiom already covers.
pub async fn list_assigned_updated_since(
    db: &DatabaseConnection,
    agent_id: &str,
    since: &str,
) -> Result<Vec<TaskEventRow>, DbErr> {
    let rows = Entity::find()
        .filter(Column::AssignedTo.eq(agent_id))
        .filter(Column::UpdatedAt.gt(since))
        .order_by_asc(Column::UpdatedAt)
        .all(db)
        .await?;
    Ok(rows.into_iter().map(task_event_from_model).collect())
}

/// Unassigned tasks NOT in `excluded_statuses`, touched since `since`
/// (`updated_at > since`), oldest first. Feeds
/// `_collect_unassigned_task_events_for`'s `unassigned_task_appeared`
/// stream.
///
/// BL-R10-2: keyed on `updated_at` (the transition-to-unassigned time),
/// not `created_at` -- a task orphaned by terminate/purge/reassignment
/// keeps its original creation time, so a `created_at`-keyed catch-up
/// would never re-surface a task that only became claimable afterwards.
///
/// `excluded_statuses` is a caller-supplied slice, NOT a constant in
/// this crate -- `conexus-wakeloop::hold_ladder`'s sibling module
/// `idle_reminder` already has its OWN 4-element terminal-status
/// constant (with an extra single-L "canceled" spelling) for a
/// genuinely different Python feature (`core/idle_reminder.py`). This
/// query's actual Python source (`_collect_unassigned_task_events_for`)
/// uses `features.task_queries.TERMINAL_TASK_STATUSES`, a DIFFERENT
/// 3-element set (`completed`/`cancelled`/`failed`) -- hardcoding
/// either constant here would risk silently reusing the wrong one at a
/// future call site. The caller owns which set applies.
pub async fn list_unassigned_active_updated_since(
    db: &DatabaseConnection,
    since: &str,
    excluded_statuses: &[&str],
) -> Result<Vec<TaskEventRow>, DbErr> {
    let rows = Entity::find()
        .filter(Column::AssignedTo.is_null())
        .filter(Column::Status.is_not_in(excluded_statuses.iter().copied()))
        .filter(Column::UpdatedAt.gt(since))
        .order_by_asc(Column::UpdatedAt)
        .all(db)
        .await?;
    Ok(rows.into_iter().map(task_event_from_model).collect())
}

/// Count of tasks assigned to `agent_id` NOT in `excluded_statuses`.
/// Port of `resources/status.py::render_status`'s `unfinished_tasks`
/// counter. `excluded_statuses` is a caller-supplied slice, same
/// discipline as [`list_unassigned_active_updated_since`] -- this
/// query's own Python source hardcodes its own local
/// `_TERMINAL_TASK_STATUSES = ("completed", "cancelled", "failed")`,
/// a separate tuple from every other terminal-status constant in this
/// codebase, not a shared import.
pub async fn count_active_by_assignee(
    db: &DatabaseConnection,
    agent_id: &str,
    excluded_statuses: &[&str],
) -> Result<i64, DbErr> {
    let count = Entity::find()
        .filter(Column::AssignedTo.eq(agent_id))
        .filter(Column::Status.is_not_in(excluded_statuses.iter().copied()))
        .count(db)
        .await?;
    Ok(count as i64)
}

/// Parameters for [`create`]. Unlike every other repository's
/// `create()` in this crate (which all require the caller to supply
/// the primary id), `task_id` is genuinely optional here — matching
/// Python's `create()`, which mints one via [`generate_task_id`] when
/// omitted. This one exception is deliberate: Python's own id
/// generation is the load-bearing, previously-buggy piece (see the
/// module doc), not an incidental default worth flattening away for
/// cross-repository consistency.
pub struct NewTask<'a> {
    pub task_id: Option<&'a str>,
    pub title: &'a str,
    pub description: Option<&'a str>,
    pub assigned_to: Option<&'a str>,
    pub created_by: &'a str,
    pub status: &'a str,
    pub priority: &'a str,
    pub parent_task: Option<&'a str>,
    pub child_tasks: Option<&'a [String]>,
    pub depends_on_tasks: Option<&'a [String]>,
    pub notes: Option<&'a [TaskNote]>,
    pub now: &'a str,
}

/// INSERT a task, minting a [`generate_task_id`] id if
/// `task.task_id` is `None`. A duplicate id (caller-supplied or, in
/// the astronomically unlikely collision case, minted) surfaces as a
/// real `DbErr` — deliberately NOT swallowed or wrapped, matching
/// Python's explicit choice. Built via sea-orm's typed `Entity::insert`,
/// then re-read via the async [`get_by_id`] ("async calls async" —
/// unlike `scheduled_directive_repository::create`, which builds its
/// returned row straight from its INSERT params, this mirrors the
/// re-`SELECT` [`create_in_transaction`] already did).
pub async fn create(db: &DatabaseConnection, task: NewTask<'_>) -> Result<TaskRow, DbErr> {
    let minted;
    let task_id = match task.task_id {
        Some(id) => id,
        None => {
            minted = generate_task_id();
            &minted
        }
    };

    let child_json = task
        .child_tasks
        .map(|v| serde_json::to_string(v).expect("Vec<String> always serializes"));
    let depends_json = task
        .depends_on_tasks
        .map(|v| serde_json::to_string(v).expect("Vec<String> always serializes"));
    let notes_json = task
        .notes
        .map(|v| serde_json::to_string(v).expect("Vec<TaskNote> always serializes"));

    let am = task::ActiveModel {
        task_id: Set(task_id.to_string()),
        title: Set(task.title.to_string()),
        description: Set(task.description.map(String::from)),
        assigned_to: Set(task.assigned_to.map(String::from)),
        created_by: Set(task.created_by.to_string()),
        status: Set(task.status.to_string()),
        priority: Set(task.priority.to_string()),
        created_at: Set(task.now.to_string()),
        updated_at: Set(task.now.to_string()),
        parent_task: Set(task.parent_task.map(String::from)),
        child_tasks: Set(child_json),
        depends_on_tasks: Set(depends_json),
        notes: Set(notes_json),
    };

    Entity::insert(am).exec(db).await?;

    Ok(get_by_id(db, task_id)
        .await?
        .expect("row was just written under this same connection"))
}

/// Deliberately-still-sync twin of [`create`] for every caller that
/// writes inside an in-flight, uncommitted `rusqlite::Transaction`
/// shared with the caller's own other writes -- same rationale as
/// [`update_fields_in_transaction`]'s own doc: a write through
/// `sea_orm_db`'s separate connection pool here would land OUTSIDE the
/// caller's atomic transaction boundary entirely, not inside it. Not
/// deleted when every remaining caller is itself eventually converted
/// to a sea-orm transaction -- delete this helper THEN, once nothing
/// calls it.
pub fn create_in_transaction(conn: &Connection, task: NewTask) -> Result<TaskRow> {
    let minted;
    let task_id = match task.task_id {
        Some(id) => id,
        None => {
            minted = generate_task_id();
            &minted
        }
    };

    let child_json = task
        .child_tasks
        .map(|v| serde_json::to_string(v).expect("Vec<String> always serializes"));
    let depends_json = task
        .depends_on_tasks
        .map(|v| serde_json::to_string(v).expect("Vec<String> always serializes"));
    let notes_json = task
        .notes
        .map(|v| serde_json::to_string(v).expect("Vec<TaskNote> always serializes"));

    conn.execute(
        "INSERT INTO tasks (task_id, title, description, assigned_to, created_by, status, priority, \
         created_at, updated_at, parent_task, child_tasks, depends_on_tasks, notes) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?9, ?10, ?11, ?12)",
        (
            task_id,
            task.title,
            task.description,
            task.assigned_to,
            task.created_by,
            task.status,
            task.priority,
            task.now,
            task.parent_task,
            child_json,
            depends_json,
            notes_json,
        ),
    )?;

    Ok(get_by_id_in_transaction(conn, task_id)?
        .expect("row was just written under this same connection"))
}

/// The allowlisted columns [`update_fields`] may touch — a closed
/// struct mirroring Python's `_MUTABLE_FIELDS`/`_sanitise_fields`
/// allowlist. `title`/`status`/`priority` are non-nullable columns
/// (plain `Option` — touch or don't); the rest use
/// [`NullableUpdate`] (reused from `scheduled_directive_repository`,
/// not re-defined) since they're nullable columns needing a real
/// 3-state "unchanged / clear / set".
#[derive(Debug, Clone, Default)]
pub struct TaskFields<'a> {
    pub title: Option<&'a str>,
    pub description: NullableUpdate<String>,
    pub assigned_to: NullableUpdate<String>,
    pub status: Option<&'a str>,
    pub priority: Option<&'a str>,
    pub parent_task: NullableUpdate<String>,
    pub child_tasks: NullableUpdate<Vec<String>>,
    pub depends_on_tasks: NullableUpdate<Vec<String>>,
    pub notes: NullableUpdate<Vec<TaskNote>>,
}

/// The literal SQLite trigger names/checks
/// `trg_tasks_terminal_state_guard`'s `RAISE(ABORT, ...)` message
/// against — a static string embedded in `schema.rs`'s DDL, since
/// SQLite's trigger grammar only accepts a literal for `RAISE`, never
/// an interpolated value. Matched by substring, matching Python's own
/// `GUARD_MARKER in str(e)` check exactly (verified against the real
/// migration source, `0025_terminal_task_guard_trigger.py`).
const GUARD_MARKER: &str = "terminal_task_guard";

/// The DB-level terminal-state guard trigger refused a write — the
/// task is `completed`/`cancelled`/`failed` and the attempted change
/// touches a frozen column (`status`/`priority`/`notes`/`title`/
/// `description`, or reassigning `assigned_to` to a non-NULL value;
/// CLEARING `assigned_to` to `NULL`, and touching `child_tasks`/
/// `depends_on_tasks`/`parent_task`, are explicitly exempt and never
/// trigger this).
#[derive(Debug)]
pub struct TerminalTaskWriteBlocked {
    pub task_id: String,
    pub message: String,
}

impl std::fmt::Display for TerminalTaskWriteBlocked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "cannot modify task {:?}: {}", self.task_id, self.message)
    }
}

impl std::error::Error for TerminalTaskWriteBlocked {}

/// Failure modes of [`update_fields`]/[`bulk_update_fields`] and their
/// `_in_transaction` twins. Generic over the underlying DB error type
/// (`E`, defaulting to `rusqlite::Error` so every existing sync
/// call/match site keeps compiling unchanged) rather than two
/// separately-named enums, since [`update_fields_in_transaction`] and
/// [`update_fields`] classify the identical
/// `trg_tasks_terminal_state_guard` refusal, just against a different
/// backend's error type (`rusqlite::Error` vs sea-orm's `DbErr`).
#[derive(Debug)]
pub enum UpdateTaskError<E = rusqlite::Error> {
    TerminalTaskWriteBlocked(TerminalTaskWriteBlocked),
    Db(E),
}

impl<E: std::fmt::Display> std::fmt::Display for UpdateTaskError<E> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UpdateTaskError::TerminalTaskWriteBlocked(e) => write!(f, "{e}"),
            UpdateTaskError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl<E: std::fmt::Debug + std::fmt::Display> std::error::Error for UpdateTaskError<E> {}

/// Classifies a failed `UPDATE tasks` as either the guard trigger
/// firing (checked via [`GUARD_MARKER`] substring-matching the
/// trigger's `RAISE` message, exactly mirroring Python's
/// `GUARD_MARKER in str(e)`) or a genuine unrelated DB error —
/// crucially, an ordinary FK/UNIQUE violation must NOT be
/// misclassified as the guard firing just because it's also a
/// `SqliteFailure`.
fn classify_update_error(task_id: &str, e: rusqlite::Error) -> UpdateTaskError {
    if let rusqlite::Error::SqliteFailure(_, Some(msg)) = &e {
        if msg.contains(GUARD_MARKER) {
            return UpdateTaskError::TerminalTaskWriteBlocked(TerminalTaskWriteBlocked {
                task_id: task_id.to_string(),
                message: msg.clone(),
            });
        }
    }
    UpdateTaskError::Db(e)
}

/// Async/sea-orm counterpart of [`classify_update_error`] -- same
/// GUARD_MARKER substring classification, matched against `DbErr`'s
/// own `Display` text rather than a raw `rusqlite::Error`'s
/// `SqliteFailure` message. The marker text is proven to survive
/// sea-orm's `DbErr` wrapping intact (`task_comments_repository`'s own
/// PR2 precedent, verified against the real trigger).
fn classify_update_error_orm(task_id: &str, e: DbErr) -> UpdateTaskError<DbErr> {
    let msg = e.to_string();
    if msg.contains(GUARD_MARKER) {
        return UpdateTaskError::TerminalTaskWriteBlocked(TerminalTaskWriteBlocked {
            task_id: task_id.to_string(),
            message: msg,
        });
    }
    UpdateTaskError::Db(e)
}

/// Partial UPDATE of the allowed columns; always refreshes
/// `updated_at` regardless of whether any other field changed
/// (matching every other `update_fields` in this crate). `Ok(None)`
/// if `task_id` doesn't exist; `Err(UpdateTaskError::
/// TerminalTaskWriteBlocked)` if the write touches a column the
/// `trg_tasks_terminal_state_guard` trigger protects on a terminal
/// task (see that struct's doc for exactly which columns). Built as a
/// partial `ActiveModel` (`Set` for a changed column, `NotSet` for an
/// untouched one), same idiom as
/// `scheduled_directive_repository::update_fields`.
pub async fn update_fields(
    db: &DatabaseConnection,
    task_id: &str,
    fields: &TaskFields<'_>,
    now: &str,
) -> std::result::Result<Option<TaskRow>, UpdateTaskError<DbErr>> {
    if get_by_id(db, task_id)
        .await
        .map_err(UpdateTaskError::Db)?
        .is_none()
    {
        return Ok(None);
    }

    let mut am = task::ActiveModel {
        task_id: Set(task_id.to_string()),
        ..Default::default()
    };

    if let Some(v) = fields.title {
        am.title = Set(v.to_string());
    }
    match &fields.description {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.description = Set(None),
        NullableUpdate::Set(v) => am.description = Set(Some(v.clone())),
    }
    match &fields.assigned_to {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.assigned_to = Set(None),
        NullableUpdate::Set(v) => am.assigned_to = Set(Some(v.clone())),
    }
    if let Some(v) = fields.status {
        am.status = Set(v.to_string());
    }
    if let Some(v) = fields.priority {
        am.priority = Set(v.to_string());
    }
    match &fields.parent_task {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.parent_task = Set(None),
        NullableUpdate::Set(v) => am.parent_task = Set(Some(v.clone())),
    }
    match &fields.child_tasks {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.child_tasks = Set(None),
        NullableUpdate::Set(v) => {
            am.child_tasks = Set(Some(
                serde_json::to_string(v).expect("Vec<String> always serializes"),
            ));
        }
    }
    match &fields.depends_on_tasks {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.depends_on_tasks = Set(None),
        NullableUpdate::Set(v) => {
            am.depends_on_tasks = Set(Some(
                serde_json::to_string(v).expect("Vec<String> always serializes"),
            ));
        }
    }
    match &fields.notes {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => am.notes = Set(None),
        NullableUpdate::Set(v) => {
            am.notes = Set(Some(
                serde_json::to_string(v).expect("Vec<TaskNote> always serializes"),
            ));
        }
    }

    am.updated_at = Set(now.to_string());

    let model = task::Entity::update(am)
        .exec(db)
        .await
        .map_err(|e| classify_update_error_orm(task_id, e))?;

    Ok(Some(task_row_from_model(model)))
}

/// Deliberately-still-sync twin of [`update_fields`] for every caller
/// that writes inside an in-flight, uncommitted `rusqlite::Transaction`
/// shared with the caller's own other writes -- same rationale as
/// [`get_by_id_in_transaction`]'s own doc: a write through
/// `sea_orm_db`'s separate connection pool here would land OUTSIDE the
/// caller's atomic transaction boundary entirely, not inside it. Not
/// deleted when every remaining caller is itself eventually converted
/// to a sea-orm transaction -- delete this helper THEN, once nothing
/// calls it.
pub fn update_fields_in_transaction(
    conn: &Connection,
    task_id: &str,
    fields: &TaskFields,
    now: &str,
) -> std::result::Result<Option<TaskRow>, UpdateTaskError> {
    if get_by_id_in_transaction(conn, task_id)
        .map_err(UpdateTaskError::Db)?
        .is_none()
    {
        return Ok(None);
    }

    let mut set_clauses: Vec<&str> = Vec::new();
    let mut params: Vec<Box<dyn ToSql>> = Vec::new();

    if let Some(v) = fields.title {
        set_clauses.push("title = ?");
        params.push(Box::new(v.to_string()));
    }
    match &fields.description {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("description = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("description = ?");
            params.push(Box::new(v.clone()));
        }
    }
    match &fields.assigned_to {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("assigned_to = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("assigned_to = ?");
            params.push(Box::new(v.clone()));
        }
    }
    if let Some(v) = fields.status {
        set_clauses.push("status = ?");
        params.push(Box::new(v.to_string()));
    }
    if let Some(v) = fields.priority {
        set_clauses.push("priority = ?");
        params.push(Box::new(v.to_string()));
    }
    match &fields.parent_task {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("parent_task = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("parent_task = ?");
            params.push(Box::new(v.clone()));
        }
    }
    match &fields.child_tasks {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("child_tasks = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("child_tasks = ?");
            params.push(Box::new(
                serde_json::to_string(v).expect("Vec<String> always serializes"),
            ));
        }
    }
    match &fields.depends_on_tasks {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("depends_on_tasks = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("depends_on_tasks = ?");
            params.push(Box::new(
                serde_json::to_string(v).expect("Vec<String> always serializes"),
            ));
        }
    }
    match &fields.notes {
        NullableUpdate::Unchanged => {}
        NullableUpdate::Clear => set_clauses.push("notes = NULL"),
        NullableUpdate::Set(v) => {
            set_clauses.push("notes = ?");
            params.push(Box::new(
                serde_json::to_string(v).expect("Vec<TaskNote> always serializes"),
            ));
        }
    }

    set_clauses.push("updated_at = ?");
    params.push(Box::new(now.to_string()));
    params.push(Box::new(task_id.to_string()));

    let sql = format!(
        "UPDATE tasks SET {} WHERE task_id = ?",
        set_clauses.join(", ")
    );
    let param_refs: Vec<&dyn ToSql> = params.iter().map(|b| b.as_ref()).collect();
    conn.execute(&sql, param_refs.as_slice())
        .map_err(|e| classify_update_error(task_id, e))?;

    get_by_id_in_transaction(conn, task_id).map_err(UpdateTaskError::Db)
}

/// Applies the SAME field-set update across N task ids in a loop —
/// unknown ids are silently skipped (matching Python), collecting the
/// rows that WERE updated. Unlike Python (which fires exactly one
/// `"task.bulk_updated"` event regardless of row count — a
/// composition-layer/EventBus concern this crate doesn't implement),
/// this stops at the first genuine error (including the first
/// terminal-task-guard refusal) rather than silently skipping it —
/// the safest interpretation absent an explicit "continue past
/// per-row failures" contract from Python for this specific case.
pub async fn bulk_update_fields(
    db: &DatabaseConnection,
    task_ids: &[&str],
    fields: &TaskFields<'_>,
    now: &str,
) -> std::result::Result<Vec<TaskRow>, UpdateTaskError<DbErr>> {
    let mut updated = Vec::new();
    for task_id in task_ids {
        if let Some(row) = update_fields(db, task_id, fields, now).await? {
            updated.push(row);
        }
    }
    Ok(updated)
}

/// `true` iff a row existed and was removed. No cross-table cascade
/// (agent `current_task` pointer cleanup, descendant deletes) — that
/// choreography lives one layer up (Python: `app.routes`/
/// `task_tools.py`; Rust: a future `conexus-tools` composition using
/// this function plus `AgentRepository::clear_current_task_for`).
/// Built via sea-orm's typed `Entity::delete_by_id`, same idiom as
/// this module's other single-row operations.
pub async fn delete(db: &DatabaseConnection, task_id: &str) -> Result<bool, DbErr> {
    let result = Entity::delete_by_id(task_id).exec(db).await?;
    Ok(result.rows_affected > 0)
}

/// Deliberately-still-sync twin of [`delete`] for every caller that
/// writes inside an in-flight, uncommitted `rusqlite::Transaction`
/// shared with the caller's own other writes -- same rationale as
/// [`update_fields_in_transaction`]'s own doc: a write through
/// `sea_orm_db`'s separate connection pool here would land OUTSIDE the
/// caller's atomic transaction boundary entirely, not inside it. Not
/// deleted when every remaining caller is itself eventually converted
/// to a sea-orm transaction -- delete this helper THEN, once nothing
/// calls it.
pub fn delete_in_transaction(conn: &Connection, task_id: &str) -> Result<bool> {
    let changed = conn.execute("DELETE FROM tasks WHERE task_id = ?1", [task_id])?;
    Ok(changed > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    /// A file-backed DB opened as BOTH a `rusqlite::Connection` (to
    /// seed rows through the still-sync [`create_in_transaction`]) and
    /// a sea-orm `DatabaseConnection` (to exercise the now-converted
    /// [`count_by_status`]/[`count_active_by_assignee`]) -- the same
    /// dual-connection recipe `pending_directive_repository`/
    /// `scheduled_directive_repository`'s own tests use, since an
    /// in-memory `:memory:` DB can't be shared across two separate
    /// connection handles the way a real file can.
    async fn test_conn_with_sea_orm() -> (tempfile::TempDir, Connection, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        init_schema(&conn).unwrap();
        let db = sea_orm::Database::connect(format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        (dir, conn, db)
    }

    fn new_task<'a>(id: Option<&'a str>, title: &'a str) -> NewTask<'a> {
        NewTask {
            task_id: id,
            title,
            description: None,
            assigned_to: None,
            created_by: "alice",
            status: "pending",
            priority: "medium",
            parent_task: None,
            child_tasks: None,
            depends_on_tasks: None,
            notes: None,
            now: "2026-01-01T00:00:00Z",
        }
    }

    #[test]
    fn generate_task_id_has_the_expected_shape() {
        let id = generate_task_id();
        assert!(id.starts_with("task_"));
        assert_eq!(id.len(), "task_".len() + 12);
        assert!(id["task_".len()..].chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn generate_task_id_is_unique_across_rapid_calls() {
        let ids: std::collections::HashSet<String> =
            (0..1000).map(|_| generate_task_id()).collect();
        assert_eq!(ids.len(), 1000, "1000 rapid calls must not collide");
    }

    #[tokio::test]
    async fn create_mints_a_task_id_when_omitted() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        let row = create(&db, new_task(None, "untitled")).await.unwrap();
        assert!(row.task_id.starts_with("task_"));
    }

    #[tokio::test]
    async fn create_uses_the_caller_supplied_task_id_when_given() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        let row = create(&db, new_task(Some("task_explicit"), "titled"))
            .await
            .unwrap();
        assert_eq!(row.task_id, "task_explicit");
    }

    #[tokio::test]
    async fn create_duplicate_id_is_a_real_propagated_error() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        create(&db, new_task(Some("task_dup"), "first"))
            .await
            .unwrap();
        let err = create(&db, new_task(Some("task_dup"), "second")).await;
        assert!(err.is_err());
    }

    #[tokio::test]
    async fn create_round_trips_json_list_fields() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        let children = vec!["task_a".to_string(), "task_b".to_string()];
        let deps = vec!["task_c".to_string()];
        let notes = vec![TaskNote {
            timestamp: "2026-01-01T00:00:00Z".to_string(),
            author: Some("alice".to_string()),
            content: "hi".to_string(),
        }];
        let mut task = new_task(Some("task_1"), "with lists");
        task.child_tasks = Some(&children);
        task.depends_on_tasks = Some(&deps);
        task.notes = Some(&notes);

        let row = create(&db, task).await.unwrap();
        assert_eq!(row.child_tasks, Some(children));
        assert_eq!(row.depends_on_tasks, Some(deps));
        assert_eq!(row.notes, Some(notes));
    }

    #[tokio::test]
    async fn get_by_id_returns_none_for_unknown_task() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert_eq!(get_by_id(&db, "nope").await.unwrap(), None);
    }

    #[tokio::test]
    async fn list_all_orders_newest_first_and_respects_limit() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t1 = new_task(Some("task_1"), "first");
        t1.now = "2026-01-01T00:00:00Z";
        create_in_transaction(&conn, t1).unwrap();
        // Only one root task is allowed (idx_tasks_single_root) --
        // t2 is a child of t1 so this test can seed 2 sibling rows
        // without tripping that unrelated invariant.
        let mut t2 = new_task(Some("task_2"), "second");
        t2.now = "2026-01-02T00:00:00Z";
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();

        let all = list_all(&db, None).await.unwrap();
        assert_eq!(
            all.iter().map(|t| t.task_id.as_str()).collect::<Vec<_>>(),
            vec!["task_2", "task_1"]
        );

        let limited = list_all(&db, Some(1)).await.unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].task_id, "task_2");
    }

    #[tokio::test]
    async fn count_by_status_groups_correctly() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t1 = new_task(Some("task_1"), "a");
        t1.status = "pending";
        create_in_transaction(&conn, t1).unwrap();
        // t2/t3 are children of t1 -- only one root task is allowed
        // (idx_tasks_single_root), unrelated to what this test checks.
        let mut t2 = new_task(Some("task_2"), "b");
        t2.status = "pending";
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();
        let mut t3 = new_task(Some("task_3"), "c");
        t3.status = "completed";
        t3.parent_task = Some("task_1");
        create_in_transaction(&conn, t3).unwrap();

        let counts = count_by_status(&db).await.unwrap();
        assert_eq!(counts.get("pending"), Some(&2));
        assert_eq!(counts.get("completed"), Some(&1));
    }

    #[tokio::test]
    async fn list_by_agent_filters_by_assignee_and_status() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t1 = new_task(Some("task_1"), "a");
        t1.assigned_to = Some("alice");
        t1.status = "pending";
        create_in_transaction(&conn, t1).unwrap();
        // t2/t3 are children of t1 -- only one root task is allowed
        // (idx_tasks_single_root), unrelated to what this test checks.
        let mut t2 = new_task(Some("task_2"), "b");
        t2.assigned_to = Some("alice");
        t2.status = "completed";
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();
        let mut t3 = new_task(Some("task_3"), "c");
        t3.assigned_to = Some("bob");
        t3.parent_task = Some("task_1");
        create_in_transaction(&conn, t3).unwrap();

        let for_alice = list_by_agent(&db, "alice", None, None).await.unwrap();
        assert_eq!(for_alice.len(), 2);

        let alice_pending = list_by_agent(&db, "alice", Some("pending"), None)
            .await
            .unwrap();
        assert_eq!(alice_pending.len(), 1);
        assert_eq!(alice_pending[0].task_id, "task_1");
    }

    #[tokio::test]
    async fn update_fields_unknown_task_returns_none() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        let result =
            update_fields(&db, "nope", &TaskFields::default(), "2026-01-01T00:00:00Z").await;
        assert_eq!(result.unwrap(), None);
    }

    #[tokio::test]
    async fn update_fields_always_bumps_updated_at() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "a")).unwrap();

        let row = update_fields(
            &db,
            "task_1",
            &TaskFields::default(),
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(row.updated_at, "2026-01-02T00:00:00Z");
    }

    #[tokio::test]
    async fn update_fields_can_change_title_status_priority() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "old title")).unwrap();

        let row = update_fields(
            &db,
            "task_1",
            &TaskFields {
                title: Some("new title"),
                status: Some("in_progress"),
                priority: Some("high"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(row.title, "new title");
        assert_eq!(row.status, "in_progress");
        assert_eq!(row.priority, "high");
    }

    #[tokio::test]
    async fn update_fields_nullable_update_can_clear_and_set_assigned_to() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut task = new_task(Some("task_1"), "a");
        task.assigned_to = Some("alice");
        create_in_transaction(&conn, task).unwrap();

        let cleared = update_fields(
            &db,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Clear,
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(cleared.assigned_to, None);

        let reassigned = update_fields(
            &db,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Set("bob".to_string()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(reassigned.assigned_to.as_deref(), Some("bob"));
    }

    #[tokio::test]
    async fn update_fields_child_tasks_json_round_trips() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "a")).unwrap();

        let children = vec!["task_2".to_string(), "task_3".to_string()];
        let row = update_fields(
            &db,
            "task_1",
            &TaskFields {
                child_tasks: NullableUpdate::Set(children.clone()),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(row.child_tasks, Some(children));
    }

    #[tokio::test]
    async fn delete_removes_row_and_returns_true() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "a")).unwrap();
        assert!(delete(&db, "task_1").await.unwrap());
        assert_eq!(get_by_id(&db, "task_1").await.unwrap(), None);
    }

    #[tokio::test]
    async fn delete_missing_task_returns_false() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert!(!delete(&db, "nope").await.unwrap());
    }

    #[tokio::test]
    async fn single_root_task_index_rejects_a_second_root() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        // Both tasks have parent_task = NULL -> both are "roots".
        create(&db, new_task(Some("task_1"), "first root"))
            .await
            .unwrap();
        let err = create(&db, new_task(Some("task_2"), "second root")).await;
        assert!(
            err.is_err(),
            "the schema-level expression index must reject a second root task"
        );
    }

    #[tokio::test]
    async fn non_root_tasks_are_unconstrained_by_the_single_root_index() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_root"), "root")).unwrap();

        let mut child1 = new_task(Some("task_child1"), "child 1");
        child1.parent_task = Some("task_root");
        create_in_transaction(&conn, child1).unwrap();

        let mut child2 = new_task(Some("task_child2"), "child 2");
        child2.parent_task = Some("task_root");
        create_in_transaction(&conn, child2).unwrap();

        // Two non-root tasks with parent_task set must NOT collide.
        assert!(get_by_id(&db, "task_child1").await.unwrap().is_some());
        assert!(get_by_id(&db, "task_child2").await.unwrap().is_some());
    }

    /// `parent` avoids tripping `idx_tasks_single_root` when a test
    /// needs more than one task and only the FIRST should be a root.
    async fn terminal_task_with_parent(
        conn: &Connection,
        db: &DatabaseConnection,
        task_id: &str,
        parent: Option<&str>,
    ) {
        let mut t = new_task(Some(task_id), "will complete");
        t.status = "in_progress";
        t.parent_task = parent;
        create_in_transaction(conn, t).unwrap();
        update_fields(
            db,
            task_id,
            &TaskFields {
                status: Some("completed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();
    }

    async fn terminal_task(conn: &Connection, db: &DatabaseConnection, task_id: &str) {
        terminal_task_with_parent(conn, db, task_id, None).await;
    }

    fn assert_blocked<E: std::fmt::Debug>(
        result: std::result::Result<Option<TaskRow>, UpdateTaskError<E>>,
        task_id: &str,
    ) {
        match result {
            Err(UpdateTaskError::TerminalTaskWriteBlocked(e)) => assert_eq!(e.task_id, task_id),
            other => panic!("expected TerminalTaskWriteBlocked, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn terminal_task_rejects_status_change() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        terminal_task(&conn, &db, "task_1").await;
        let result = update_fields(
            &db,
            "task_1",
            &TaskFields {
                status: Some("in_progress"),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await;
        assert_blocked(result, "task_1");
    }

    #[tokio::test]
    async fn terminal_task_rejects_priority_title_description_notes_changes() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_shared_root"), "root")).unwrap();
        for (label, fields) in [
            (
                "priority",
                TaskFields {
                    priority: Some("high"),
                    ..Default::default()
                },
            ),
            (
                "title",
                TaskFields {
                    title: Some("new title"),
                    ..Default::default()
                },
            ),
            (
                "description",
                TaskFields {
                    description: NullableUpdate::Set("new desc".to_string()),
                    ..Default::default()
                },
            ),
            (
                "notes",
                TaskFields {
                    notes: NullableUpdate::Set(vec![TaskNote {
                        timestamp: "2026-01-01T00:00:00Z".to_string(),
                        author: None,
                        content: "x".to_string(),
                    }]),
                    ..Default::default()
                },
            ),
        ] {
            let task_id = format!("task_{label}");
            terminal_task_with_parent(&conn, &db, &task_id, Some("task_shared_root")).await;
            let result = update_fields(&db, &task_id, &fields, "2026-01-03T00:00:00Z").await;
            assert_blocked(result, &task_id);
        }
    }

    #[tokio::test]
    async fn terminal_task_rejects_reassigning_to_a_new_agent() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        terminal_task(&conn, &db, "task_1").await;
        let result = update_fields(
            &db,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Set("bob".to_string()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await;
        assert_blocked(result, "task_1");
    }

    #[tokio::test]
    async fn terminal_task_allows_clearing_assigned_to() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "will complete");
        t.status = "in_progress";
        t.assigned_to = Some("alice");
        create_in_transaction(&conn, t).unwrap();
        update_fields(
            &db,
            "task_1",
            &TaskFields {
                status: Some("completed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();

        let row = update_fields(
            &db,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Clear,
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            row.assigned_to, None,
            "clearing assigned_to on a terminal task must be allowed"
        );
    }

    #[tokio::test]
    async fn terminal_task_allows_child_tasks_depends_on_and_parent_task_changes() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        terminal_task(&conn, &db, "task_1").await;
        let mut other = new_task(Some("task_other"), "sibling");
        other.parent_task = Some("task_1");
        create_in_transaction(&conn, other).unwrap();

        let children = vec!["task_x".to_string()];
        let row = update_fields(
            &db,
            "task_1",
            &TaskFields {
                child_tasks: NullableUpdate::Set(children.clone()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            row.child_tasks,
            Some(children),
            "child_tasks must be writable even on a terminal task"
        );
    }

    #[tokio::test]
    async fn non_terminal_task_is_fully_mutable_as_before() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "still open")).unwrap();

        let row = update_fields(
            &db,
            "task_1",
            &TaskFields {
                status: Some("in_progress"),
                title: Some("renamed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(row.status, "in_progress");
        assert_eq!(row.title, "renamed");
    }

    #[tokio::test]
    async fn bulk_update_fields_updates_every_existing_id_and_skips_unknown_ones() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        create_in_transaction(&conn, new_task(Some("task_1"), "a")).unwrap();
        let mut t2 = new_task(Some("task_2"), "b");
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();

        let updated = bulk_update_fields(
            &db,
            &["task_1", "task_2", "task_missing"],
            &TaskFields {
                status: Some("in_progress"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .await
        .unwrap();

        assert_eq!(
            updated.len(),
            2,
            "the unknown id must be silently skipped, not an error"
        );
        assert!(updated.iter().all(|t| t.status == "in_progress"));
    }

    #[tokio::test]
    async fn bulk_update_fields_stops_on_the_first_terminal_task_guard_refusal() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        terminal_task(&conn, &db, "task_1").await;
        let mut t2 = new_task(Some("task_2"), "b");
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();

        let result = bulk_update_fields(
            &db,
            &["task_1", "task_2"],
            &TaskFields {
                status: Some("cancelled"),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .await;
        assert!(matches!(
            result,
            Err(UpdateTaskError::TerminalTaskWriteBlocked(_))
        ));
    }

    fn set_updated_at(conn: &Connection, task_id: &str, updated_at: &str) {
        conn.execute(
            "UPDATE tasks SET updated_at = ?1 WHERE task_id = ?2",
            (updated_at, task_id),
        )
        .unwrap();
    }

    // -- list_assigned_updated_since ------------------------------------

    #[tokio::test]
    async fn list_assigned_updated_since_excludes_rows_at_or_before_the_cursor() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "a");
        t.assigned_to = Some("alice");
        create_in_transaction(&conn, t).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:00Z");

        assert!(
            list_assigned_updated_since(&db, "alice", "2026-01-01T00:00:00Z")
                .await
                .unwrap()
                .is_empty()
        );
        let after = list_assigned_updated_since(&db, "alice", "2025-12-31T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].task_id, "task_1");
    }

    #[tokio::test]
    async fn list_assigned_updated_since_ignores_other_agents_tasks() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "a");
        t.assigned_to = Some("bob");
        create_in_transaction(&conn, t).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:01Z");

        assert!(
            list_assigned_updated_since(&db, "alice", "2025-01-01T00:00:00Z")
                .await
                .unwrap()
                .is_empty()
        );
    }

    #[tokio::test]
    async fn list_assigned_updated_since_is_oldest_first() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t1 = new_task(Some("task_1"), "first");
        t1.assigned_to = Some("alice");
        create_in_transaction(&conn, t1).unwrap();
        let mut t2 = new_task(Some("task_2"), "second");
        t2.assigned_to = Some("alice");
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:02Z");
        set_updated_at(&conn, "task_2", "2026-01-01T00:00:01Z");

        let rows = list_assigned_updated_since(&db, "alice", "2025-01-01T00:00:00Z")
            .await
            .unwrap();
        assert_eq!(rows[0].task_id, "task_2");
        assert_eq!(rows[1].task_id, "task_1");
    }

    // -- list_unassigned_active_updated_since ----------------------------

    #[tokio::test]
    async fn list_unassigned_active_updated_since_excludes_assigned_tasks() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "a");
        t.assigned_to = Some("alice");
        create_in_transaction(&conn, t).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:00Z");

        assert!(list_unassigned_active_updated_since(
            &db,
            "2025-01-01T00:00:00Z",
            &["completed", "cancelled", "failed"],
        )
        .await
        .unwrap()
        .is_empty());
    }

    #[tokio::test]
    async fn list_unassigned_active_updated_since_excludes_the_given_terminal_statuses() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "done");
        t.status = "completed";
        create_in_transaction(&conn, t).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:00Z");

        assert!(list_unassigned_active_updated_since(
            &db,
            "2025-01-01T00:00:00Z",
            &["completed", "cancelled", "failed"],
        )
        .await
        .unwrap()
        .is_empty());
    }

    #[tokio::test]
    async fn list_unassigned_active_updated_since_returns_claimable_tasks() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let t = new_task(Some("task_1"), "up for grabs");
        create_in_transaction(&conn, t).unwrap();
        set_updated_at(&conn, "task_1", "2026-01-01T00:00:00Z");

        let rows = list_unassigned_active_updated_since(
            &db,
            "2025-01-01T00:00:00Z",
            &["completed", "cancelled", "failed"],
        )
        .await
        .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].task_id, "task_1");
    }

    // -- count_active_by_assignee -----------------------------------

    #[tokio::test]
    async fn count_active_by_assignee_counts_only_this_agents_non_terminal_tasks() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t1 = new_task(Some("task_1"), "active for alice");
        t1.assigned_to = Some("alice");
        create_in_transaction(&conn, t1).unwrap();
        let mut t2 = new_task(Some("task_2"), "active for bob");
        t2.assigned_to = Some("bob");
        t2.parent_task = Some("task_1");
        create_in_transaction(&conn, t2).unwrap();

        assert_eq!(
            count_active_by_assignee(&db, "alice", &["completed", "cancelled", "failed"])
                .await
                .unwrap(),
            1
        );
    }

    #[tokio::test]
    async fn count_active_by_assignee_excludes_the_given_terminal_statuses() {
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let mut t = new_task(Some("task_1"), "done");
        t.assigned_to = Some("alice");
        t.status = "completed";
        create_in_transaction(&conn, t).unwrap();

        assert_eq!(
            count_active_by_assignee(&db, "alice", &["completed", "cancelled", "failed"])
                .await
                .unwrap(),
            0
        );
    }

    #[tokio::test]
    async fn count_active_by_assignee_is_zero_for_an_agent_with_no_tasks() {
        let (_dir, _conn, db) = test_conn_with_sea_orm().await;
        assert_eq!(
            count_active_by_assignee(&db, "nobody", &["completed", "cancelled", "failed"])
                .await
                .unwrap(),
            0
        );
    }

    #[tokio::test]
    async fn list_unassigned_active_updated_since_keys_on_updated_at_not_created_at() {
        // BL-R10-2: a task orphaned by reassignment keeps its original
        // created_at but must still surface once its updated_at (the
        // transition-to-unassigned time) crosses the cursor.
        let (_dir, conn, db) = test_conn_with_sea_orm().await;
        let t = new_task(Some("task_1"), "orphaned");
        create_in_transaction(&conn, t).unwrap();
        // created_at is "2026-01-01T00:00:00Z" (from new_task's `now`);
        // simulate a later unassign event bumping only updated_at.
        set_updated_at(&conn, "task_1", "2026-06-01T00:00:00Z");

        let rows = list_unassigned_active_updated_since(
            &db,
            "2026-03-01T00:00:00Z", // after created_at, before updated_at
            &["completed", "cancelled", "failed"],
        )
        .await
        .unwrap();
        assert_eq!(rows.len(), 1, "must surface via updated_at, not created_at");
    }
}
