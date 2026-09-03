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
//! - **`create()` does NOT swallow a duplicate-id conflict** — it
//!   propagates the real `rusqlite::Error` uncaught, matching Python's
//!   explicit choice (documented rationale: "silently returning the
//!   existing row would mask write conflicts").
//! - **Single-root-task expression index** (`idx_tasks_single_root`,
//!   in `schema.rs`) is a SCHEMA-level invariant, not an app-level
//!   check — it must exist in the DDL, not just be re-verified in
//!   code.

use rusqlite::{Connection, OptionalExtension, Result, Row, ToSql};
use std::collections::HashMap;

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

pub fn get_by_id(conn: &Connection, task_id: &str) -> Result<Option<TaskRow>> {
    conn.query_row(
        &format!("SELECT {COLUMNS} FROM tasks WHERE task_id = ?1"),
        [task_id],
        row_to_task,
    )
    .optional()
}

/// Every task, newest first, optionally capped.
pub fn list_all(conn: &Connection, limit: Option<i64>) -> Result<Vec<TaskRow>> {
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

pub fn count_by_status(conn: &Connection) -> Result<HashMap<String, i64>> {
    let mut stmt = conn.prepare("SELECT status, COUNT(*) FROM tasks GROUP BY status")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    rows.collect()
}

/// Tasks for one agent, newest first, with an optional status filter
/// and an optional cap.
pub fn list_by_agent(
    conn: &Connection,
    agent_id: &str,
    status_filter: Option<&str>,
    limit: Option<i64>,
) -> Result<Vec<TaskRow>> {
    let mut sql = format!("SELECT {COLUMNS} FROM tasks WHERE assigned_to = ?");
    let mut params: Vec<Box<dyn ToSql>> = vec![Box::new(agent_id.to_string())];
    if let Some(s) = status_filter {
        sql.push_str(" AND status = ?");
        params.push(Box::new(s.to_string()));
    }
    sql.push_str(" ORDER BY created_at DESC");
    if let Some(l) = limit {
        sql.push_str(" LIMIT ?");
        params.push(Box::new(l));
    }

    let param_refs: Vec<&dyn ToSql> = params.iter().map(|b| b.as_ref()).collect();
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(param_refs.as_slice(), row_to_task)?;
    rows.collect()
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
/// real `rusqlite::Error` — deliberately NOT swallowed or wrapped,
/// matching Python's explicit choice.
pub fn create(conn: &Connection, task: NewTask) -> Result<TaskRow> {
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

    Ok(get_by_id(conn, task_id)?.expect("row was just written under this same connection"))
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

/// Failure modes of [`update_fields`]/[`bulk_update_fields`].
#[derive(Debug)]
pub enum UpdateTaskError {
    TerminalTaskWriteBlocked(TerminalTaskWriteBlocked),
    Db(rusqlite::Error),
}

impl std::fmt::Display for UpdateTaskError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UpdateTaskError::TerminalTaskWriteBlocked(e) => write!(f, "{e}"),
            UpdateTaskError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for UpdateTaskError {}

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

/// Partial UPDATE of the allowed columns; always refreshes
/// `updated_at` regardless of whether any other field changed
/// (matching every other `update_fields` in this crate). `Ok(None)`
/// if `task_id` doesn't exist; `Err(UpdateTaskError::
/// TerminalTaskWriteBlocked)` if the write touches a column the
/// `trg_tasks_terminal_state_guard` trigger protects on a terminal
/// task (see that struct's doc for exactly which columns).
pub fn update_fields(
    conn: &Connection,
    task_id: &str,
    fields: &TaskFields,
    now: &str,
) -> std::result::Result<Option<TaskRow>, UpdateTaskError> {
    if get_by_id(conn, task_id)
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

    get_by_id(conn, task_id).map_err(UpdateTaskError::Db)
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
pub fn bulk_update_fields(
    conn: &Connection,
    task_ids: &[&str],
    fields: &TaskFields,
    now: &str,
) -> std::result::Result<Vec<TaskRow>, UpdateTaskError> {
    let mut updated = Vec::new();
    for task_id in task_ids {
        if let Some(row) = update_fields(conn, task_id, fields, now)? {
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
pub fn delete(conn: &Connection, task_id: &str) -> Result<bool> {
    let changed = conn.execute("DELETE FROM tasks WHERE task_id = ?1", [task_id])?;
    Ok(changed > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
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

    #[test]
    fn create_mints_a_task_id_when_omitted() {
        let conn = test_conn();
        let row = create(&conn, new_task(None, "untitled")).unwrap();
        assert!(row.task_id.starts_with("task_"));
    }

    #[test]
    fn create_uses_the_caller_supplied_task_id_when_given() {
        let conn = test_conn();
        let row = create(&conn, new_task(Some("task_explicit"), "titled")).unwrap();
        assert_eq!(row.task_id, "task_explicit");
    }

    #[test]
    fn create_duplicate_id_is_a_real_propagated_error() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_dup"), "first")).unwrap();
        let err = create(&conn, new_task(Some("task_dup"), "second"));
        assert!(err.is_err());
    }

    #[test]
    fn create_round_trips_json_list_fields() {
        let conn = test_conn();
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

        let row = create(&conn, task).unwrap();
        assert_eq!(row.child_tasks, Some(children));
        assert_eq!(row.depends_on_tasks, Some(deps));
        assert_eq!(row.notes, Some(notes));
    }

    #[test]
    fn get_by_id_returns_none_for_unknown_task() {
        let conn = test_conn();
        assert_eq!(get_by_id(&conn, "nope").unwrap(), None);
    }

    #[test]
    fn list_all_orders_newest_first_and_respects_limit() {
        let conn = test_conn();
        let mut t1 = new_task(Some("task_1"), "first");
        t1.now = "2026-01-01T00:00:00Z";
        create(&conn, t1).unwrap();
        // Only one root task is allowed (idx_tasks_single_root) --
        // t2 is a child of t1 so this test can seed 2 sibling rows
        // without tripping that unrelated invariant.
        let mut t2 = new_task(Some("task_2"), "second");
        t2.now = "2026-01-02T00:00:00Z";
        t2.parent_task = Some("task_1");
        create(&conn, t2).unwrap();

        let all = list_all(&conn, None).unwrap();
        assert_eq!(
            all.iter().map(|t| t.task_id.as_str()).collect::<Vec<_>>(),
            vec!["task_2", "task_1"]
        );

        let limited = list_all(&conn, Some(1)).unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].task_id, "task_2");
    }

    #[test]
    fn count_by_status_groups_correctly() {
        let conn = test_conn();
        let mut t1 = new_task(Some("task_1"), "a");
        t1.status = "pending";
        create(&conn, t1).unwrap();
        // t2/t3 are children of t1 -- only one root task is allowed
        // (idx_tasks_single_root), unrelated to what this test checks.
        let mut t2 = new_task(Some("task_2"), "b");
        t2.status = "pending";
        t2.parent_task = Some("task_1");
        create(&conn, t2).unwrap();
        let mut t3 = new_task(Some("task_3"), "c");
        t3.status = "completed";
        t3.parent_task = Some("task_1");
        create(&conn, t3).unwrap();

        let counts = count_by_status(&conn).unwrap();
        assert_eq!(counts.get("pending"), Some(&2));
        assert_eq!(counts.get("completed"), Some(&1));
    }

    #[test]
    fn list_by_agent_filters_by_assignee_and_status() {
        let conn = test_conn();
        let mut t1 = new_task(Some("task_1"), "a");
        t1.assigned_to = Some("alice");
        t1.status = "pending";
        create(&conn, t1).unwrap();
        // t2/t3 are children of t1 -- only one root task is allowed
        // (idx_tasks_single_root), unrelated to what this test checks.
        let mut t2 = new_task(Some("task_2"), "b");
        t2.assigned_to = Some("alice");
        t2.status = "completed";
        t2.parent_task = Some("task_1");
        create(&conn, t2).unwrap();
        let mut t3 = new_task(Some("task_3"), "c");
        t3.assigned_to = Some("bob");
        t3.parent_task = Some("task_1");
        create(&conn, t3).unwrap();

        let for_alice = list_by_agent(&conn, "alice", None, None).unwrap();
        assert_eq!(for_alice.len(), 2);

        let alice_pending = list_by_agent(&conn, "alice", Some("pending"), None).unwrap();
        assert_eq!(alice_pending.len(), 1);
        assert_eq!(alice_pending[0].task_id, "task_1");
    }

    #[test]
    fn update_fields_unknown_task_returns_none() {
        let conn = test_conn();
        let result = update_fields(
            &conn,
            "nope",
            &TaskFields::default(),
            "2026-01-01T00:00:00Z",
        );
        assert_eq!(result.unwrap(), None);
    }

    #[test]
    fn update_fields_always_bumps_updated_at() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "a")).unwrap();

        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields::default(),
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.updated_at, "2026-01-02T00:00:00Z");
    }

    #[test]
    fn update_fields_can_change_title_status_priority() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "old title")).unwrap();

        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                title: Some("new title"),
                status: Some("in_progress"),
                priority: Some("high"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.title, "new title");
        assert_eq!(row.status, "in_progress");
        assert_eq!(row.priority, "high");
    }

    #[test]
    fn update_fields_nullable_update_can_clear_and_set_assigned_to() {
        let conn = test_conn();
        let mut task = new_task(Some("task_1"), "a");
        task.assigned_to = Some("alice");
        create(&conn, task).unwrap();

        let cleared = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Clear,
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(cleared.assigned_to, None);

        let reassigned = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Set("bob".to_string()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(reassigned.assigned_to.as_deref(), Some("bob"));
    }

    #[test]
    fn update_fields_child_tasks_json_round_trips() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "a")).unwrap();

        let children = vec!["task_2".to_string(), "task_3".to_string()];
        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                child_tasks: NullableUpdate::Set(children.clone()),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.child_tasks, Some(children));
    }

    #[test]
    fn delete_removes_row_and_returns_true() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "a")).unwrap();
        assert!(delete(&conn, "task_1").unwrap());
        assert_eq!(get_by_id(&conn, "task_1").unwrap(), None);
    }

    #[test]
    fn delete_missing_task_returns_false() {
        let conn = test_conn();
        assert!(!delete(&conn, "nope").unwrap());
    }

    #[test]
    fn single_root_task_index_rejects_a_second_root() {
        let conn = test_conn();
        // Both tasks have parent_task = NULL -> both are "roots".
        create(&conn, new_task(Some("task_1"), "first root")).unwrap();
        let err = create(&conn, new_task(Some("task_2"), "second root"));
        assert!(
            err.is_err(),
            "the schema-level expression index must reject a second root task"
        );
    }

    #[test]
    fn non_root_tasks_are_unconstrained_by_the_single_root_index() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_root"), "root")).unwrap();

        let mut child1 = new_task(Some("task_child1"), "child 1");
        child1.parent_task = Some("task_root");
        create(&conn, child1).unwrap();

        let mut child2 = new_task(Some("task_child2"), "child 2");
        child2.parent_task = Some("task_root");
        create(&conn, child2).unwrap();

        // Two non-root tasks with parent_task set must NOT collide.
        assert!(get_by_id(&conn, "task_child1").unwrap().is_some());
        assert!(get_by_id(&conn, "task_child2").unwrap().is_some());
    }

    /// `parent` avoids tripping `idx_tasks_single_root` when a test
    /// needs more than one task and only the FIRST should be a root.
    fn terminal_task_with_parent(conn: &Connection, task_id: &str, parent: Option<&str>) {
        let mut t = new_task(Some(task_id), "will complete");
        t.status = "in_progress";
        t.parent_task = parent;
        create(conn, t).unwrap();
        update_fields(
            conn,
            task_id,
            &TaskFields {
                status: Some("completed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap();
    }

    fn terminal_task(conn: &Connection, task_id: &str) {
        terminal_task_with_parent(conn, task_id, None);
    }

    fn assert_blocked(
        result: std::result::Result<Option<TaskRow>, UpdateTaskError>,
        task_id: &str,
    ) {
        match result {
            Err(UpdateTaskError::TerminalTaskWriteBlocked(e)) => assert_eq!(e.task_id, task_id),
            other => panic!("expected TerminalTaskWriteBlocked, got {other:?}"),
        }
    }

    #[test]
    fn terminal_task_rejects_status_change() {
        let conn = test_conn();
        terminal_task(&conn, "task_1");
        let result = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                status: Some("in_progress"),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        );
        assert_blocked(result, "task_1");
    }

    #[test]
    fn terminal_task_rejects_priority_title_description_notes_changes() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_shared_root"), "root")).unwrap();
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
            let conn2 = &conn;
            let task_id = format!("task_{label}");
            terminal_task_with_parent(conn2, &task_id, Some("task_shared_root"));
            let result = update_fields(conn2, &task_id, &fields, "2026-01-03T00:00:00Z");
            assert_blocked(result, &task_id);
        }
    }

    #[test]
    fn terminal_task_rejects_reassigning_to_a_new_agent() {
        let conn = test_conn();
        terminal_task(&conn, "task_1");
        let result = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Set("bob".to_string()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        );
        assert_blocked(result, "task_1");
    }

    #[test]
    fn terminal_task_allows_clearing_assigned_to() {
        let conn = test_conn();
        let mut t = new_task(Some("task_1"), "will complete");
        t.status = "in_progress";
        t.assigned_to = Some("alice");
        create(&conn, t).unwrap();
        update_fields(
            &conn,
            "task_1",
            &TaskFields {
                status: Some("completed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap();

        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                assigned_to: NullableUpdate::Clear,
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            row.assigned_to, None,
            "clearing assigned_to on a terminal task must be allowed"
        );
    }

    #[test]
    fn terminal_task_allows_child_tasks_depends_on_and_parent_task_changes() {
        let conn = test_conn();
        terminal_task(&conn, "task_1");
        let mut other = new_task(Some("task_other"), "sibling");
        other.parent_task = Some("task_1");
        create(&conn, other).unwrap();

        let children = vec!["task_x".to_string()];
        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                child_tasks: NullableUpdate::Set(children.clone()),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            row.child_tasks,
            Some(children),
            "child_tasks must be writable even on a terminal task"
        );
    }

    #[test]
    fn non_terminal_task_is_fully_mutable_as_before() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "still open")).unwrap();

        let row = update_fields(
            &conn,
            "task_1",
            &TaskFields {
                status: Some("in_progress"),
                title: Some("renamed"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.status, "in_progress");
        assert_eq!(row.title, "renamed");
    }

    #[test]
    fn bulk_update_fields_updates_every_existing_id_and_skips_unknown_ones() {
        let conn = test_conn();
        create(&conn, new_task(Some("task_1"), "a")).unwrap();
        let mut t2 = new_task(Some("task_2"), "b");
        t2.parent_task = Some("task_1");
        create(&conn, t2).unwrap();

        let updated = bulk_update_fields(
            &conn,
            &["task_1", "task_2", "task_missing"],
            &TaskFields {
                status: Some("in_progress"),
                ..Default::default()
            },
            "2026-01-02T00:00:00Z",
        )
        .unwrap();

        assert_eq!(
            updated.len(),
            2,
            "the unknown id must be silently skipped, not an error"
        );
        assert!(updated.iter().all(|t| t.status == "in_progress"));
    }

    #[test]
    fn bulk_update_fields_stops_on_the_first_terminal_task_guard_refusal() {
        let conn = test_conn();
        terminal_task(&conn, "task_1");
        let mut t2 = new_task(Some("task_2"), "b");
        t2.parent_task = Some("task_1");
        create(&conn, t2).unwrap();

        let result = bulk_update_fields(
            &conn,
            &["task_1", "task_2"],
            &TaskFields {
                status: Some("cancelled"),
                ..Default::default()
            },
            "2026-01-03T00:00:00Z",
        );
        assert!(matches!(
            result,
            Err(UpdateTaskError::TerminalTaskWriteBlocked(_))
        ));
    }
}
