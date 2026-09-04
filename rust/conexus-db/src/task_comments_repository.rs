//! Port of `agent_mcp/db/actions/task_comments_db.py` — the
//! `task_comments` side table's sole read/write surface (migration
//! 0009's per-comment table, renamed from `task_notes` in migration
//! 0026; replaces the `tasks.notes` JSON-list-in-TEXT pattern so
//! individual comments can be edited/deleted).
//!
//! Deliberate improvement over Python's `Tuple[bool, str]` return
//! shape (matching this migration's own established precedent —
//! `task_mutation_engine::UpdateSingleTaskOutcome` replacing a
//! substring-sniffed error routing the same way): [`EditCommentError`]/
//! [`DeleteCommentError`] are closed enums the caller matches
//! exhaustively, rather than pattern-matching substrings ("not
//! found"/"owned by"/"terminal state") out of a free-form message.
//! The `NotFoundOrForbidden` variant deliberately carries NO owner
//! identity at all — SEC PF-1 requires the missing-comment and
//! foreign-comment outcomes to be indistinguishable to the caller, so
//! making that structurally true (not just "the tool layer happens to
//! discard the field") is the point.
//!
//! `list_comments_for_task`/`get_comment` (Python's read-only helpers)
//! are not ported yet — no Rust tool needs them (the 3 tools this
//! module backs are add/edit/delete only); port when a real caller
//! needs them, matching this crate's own "add what's needed" discipline.

use rusqlite::{Connection, OptionalExtension};

/// One row of the `task_comments` table.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct TaskCommentRow {
    pub note_id: i64,
    pub task_id: String,
    pub author: Option<String>,
    pub timestamp: String,
    pub text: String,
}

/// The literal SQLite trigger names/checks the `task_comments`
/// terminal-guard triggers' `RAISE(ABORT, ...)` message against —
/// shared with `task_repository`'s own copy since both match the same
/// static marker embedded in `schema.rs`'s DDL (SQLite's trigger
/// grammar only accepts a literal for `RAISE`). Matched by substring,
/// mirroring Python's `GUARD_MARKER in str(e)` check exactly.
const GUARD_MARKER: &str = "terminal_task_guard";

/// The DB-level terminal-state guard trigger refused a write — the
/// comment's parent task is `completed`/`cancelled`/`failed`.
#[derive(Debug)]
pub struct TerminalTaskWriteBlocked {
    pub task_id: String,
    pub message: String,
}

impl std::fmt::Display for TerminalTaskWriteBlocked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "cannot modify task_comments for task {:?}: {}",
            self.task_id, self.message
        )
    }
}
impl std::error::Error for TerminalTaskWriteBlocked {}

/// Failure modes of [`add_comment`].
#[derive(Debug)]
pub enum AddCommentError {
    TerminalTaskWriteBlocked(TerminalTaskWriteBlocked),
    Db(rusqlite::Error),
}

fn classify_insert_error(task_id: &str, e: rusqlite::Error) -> AddCommentError {
    if let rusqlite::Error::SqliteFailure(_, Some(msg)) = &e {
        if msg.contains(GUARD_MARKER) {
            return AddCommentError::TerminalTaskWriteBlocked(TerminalTaskWriteBlocked {
                task_id: task_id.to_string(),
                message: msg.clone(),
            });
        }
    }
    AddCommentError::Db(e)
}

/// INSERT a new comment, returning its autoincrement `note_id`.
/// Matches Python's `add_comment`'s empty-text/task_id rejection —
/// callers are expected to validate before reaching this (the tool
/// layer already does), so this stays dumb CRUD.
pub fn add_comment(
    conn: &Connection,
    task_id: &str,
    author: Option<&str>,
    text: &str,
    now: &str,
) -> Result<i64, AddCommentError> {
    conn.execute(
        "INSERT INTO task_comments (task_id, author, timestamp, text) VALUES (?1, ?2, ?3, ?4)",
        (task_id, author, now, text),
    )
    .map_err(|e| classify_insert_error(task_id, e))?;
    Ok(conn.last_insert_rowid())
}

fn get_row(conn: &Connection, note_id: i64) -> rusqlite::Result<Option<TaskCommentRow>> {
    conn.query_row(
        "SELECT note_id, task_id, author, timestamp, text FROM task_comments WHERE note_id = ?1",
        [note_id],
        |row| {
            Ok(TaskCommentRow {
                note_id: row.get(0)?,
                task_id: row.get(1)?,
                author: row.get(2)?,
                timestamp: row.get(3)?,
                text: row.get(4)?,
            })
        },
    )
    .optional()
}

fn task_status(conn: &Connection, task_id: &str) -> rusqlite::Result<Option<String>> {
    conn.query_row(
        "SELECT status FROM tasks WHERE task_id = ?1",
        [task_id],
        |row| row.get(0),
    )
    .optional()
}

const TERMINAL_STATUSES: &[&str] = &["completed", "cancelled", "failed"];

/// Failure modes of [`edit_comment`]/[`delete_comment`]. See this
/// module's doc for why `NotFoundOrForbidden` carries no owner
/// identity — SEC PF-1's comment-existence-oracle fusion.
#[derive(Debug)]
pub enum EditCommentError {
    NotFoundOrForbidden,
    Terminal {
        note_id: i64,
        task_id: String,
        status: String,
    },
    Db(rusqlite::Error),
}

pub type DeleteCommentError = EditCommentError;

/// Update a comment's text. Only the original author or `is_admin`
/// may edit. Ownership is checked BEFORE terminality (OBS-R12-2: a
/// non-owner/non-admin requester must get the same fused refusal
/// regardless of the task's status — checking terminality first would
/// let a non-owner distinguish "comment on a terminal task" from
/// "comment on a live task" from which error comes back, a new PF-1-
/// shaped oracle).
pub fn edit_comment(
    conn: &Connection,
    note_id: i64,
    requester: &str,
    new_text: &str,
    is_admin: bool,
) -> Result<(), EditCommentError> {
    let row = get_row(conn, note_id).map_err(EditCommentError::Db)?;
    let Some(row) = row else {
        return Err(EditCommentError::NotFoundOrForbidden);
    };
    if !is_admin && row.author.as_deref() != Some(requester) {
        return Err(EditCommentError::NotFoundOrForbidden);
    }
    let status = task_status(conn, &row.task_id).map_err(EditCommentError::Db)?;
    if let Some(status) = &status {
        if TERMINAL_STATUSES.contains(&status.as_str()) {
            return Err(EditCommentError::Terminal {
                note_id,
                task_id: row.task_id,
                status: status.clone(),
            });
        }
    }
    conn.execute(
        "UPDATE task_comments SET text = ?1 WHERE note_id = ?2",
        (new_text, note_id),
    )
    .map_err(|e| {
        // Defense-in-depth (Python's own comment: "never reachable in
        // normal operation" since the terminality check above already
        // refused this) -- the DB trigger firing here would otherwise
        // surface as an opaque Db error.
        if let rusqlite::Error::SqliteFailure(_, Some(msg)) = &e {
            if msg.contains(GUARD_MARKER) {
                return EditCommentError::Terminal {
                    note_id,
                    task_id: row.task_id.clone(),
                    status: status.clone().unwrap_or_default(),
                };
            }
        }
        EditCommentError::Db(e)
    })?;
    Ok(())
}

/// Delete a comment. Same ownership/moderation contract as
/// [`edit_comment`]. Unlike edit, DELETE is deliberately NOT guarded
/// by a DB trigger (matches Python: a future task-delete cascade must
/// still be able to remove a terminal task's comments) — this
/// Python-level terminality check is the ONLY guard for this call.
pub fn delete_comment(
    conn: &Connection,
    note_id: i64,
    requester: &str,
    is_admin: bool,
) -> Result<(), DeleteCommentError> {
    let row = get_row(conn, note_id).map_err(EditCommentError::Db)?;
    let Some(row) = row else {
        return Err(EditCommentError::NotFoundOrForbidden);
    };
    if !is_admin && row.author.as_deref() != Some(requester) {
        return Err(EditCommentError::NotFoundOrForbidden);
    }
    let status = task_status(conn, &row.task_id).map_err(EditCommentError::Db)?;
    if let Some(status) = status {
        if TERMINAL_STATUSES.contains(&status.as_str()) {
            return Err(EditCommentError::Terminal {
                note_id,
                task_id: row.task_id,
                status,
            });
        }
    }
    conn.execute("DELETE FROM task_comments WHERE note_id = ?1", [note_id])
        .map_err(EditCommentError::Db)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;

    fn conn_with_task(task_id: &str, status: &str) -> Connection {
        let c = Connection::open_in_memory().unwrap();
        init_schema(&c).unwrap();
        c.execute(
            "INSERT INTO tasks (task_id, title, created_by, status, priority, created_at, \
             updated_at) VALUES (?1, 'Task', 'alice', ?2, 'medium', '2026-06-01T00:00:00Z', \
             '2026-06-01T00:00:00Z')",
            (task_id, status),
        )
        .unwrap();
        c
    }

    #[test]
    fn add_comment_returns_an_incrementing_note_id() {
        let c = conn_with_task("t1", "in_progress");
        let id1 = add_comment(&c, "t1", Some("alice"), "first", "2026-06-01T00:00:00Z").unwrap();
        let id2 = add_comment(&c, "t1", Some("alice"), "second", "2026-06-01T00:01:00Z").unwrap();
        assert!(id2 > id1);
    }

    #[test]
    fn add_comment_on_a_terminal_task_is_blocked_by_the_db_trigger() {
        let c = conn_with_task("t1", "completed");
        let err =
            add_comment(&c, "t1", Some("alice"), "too late", "2026-06-01T00:00:00Z").unwrap_err();
        assert!(matches!(err, AddCommentError::TerminalTaskWriteBlocked(_)));
    }

    #[test]
    fn the_author_can_edit_their_own_comment() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        edit_comment(&c, id, "alice", "v2", false).unwrap();
        let row = get_row(&c, id).unwrap().unwrap();
        assert_eq!(row.text, "v2");
    }

    #[test]
    fn a_non_author_non_admin_edit_is_not_found_or_forbidden() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        let err = edit_comment(&c, id, "bob", "v2", false).unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
    }

    #[test]
    fn an_admin_can_edit_someone_elses_comment() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        edit_comment(&c, id, "bob", "moderated", true).unwrap();
        let row = get_row(&c, id).unwrap().unwrap();
        assert_eq!(row.text, "moderated");
    }

    #[test]
    fn editing_a_nonexistent_comment_is_not_found_or_forbidden() {
        let c = conn_with_task("t1", "in_progress");
        let err = edit_comment(&c, 999, "alice", "v2", false).unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
    }

    #[test]
    fn editing_a_comment_on_a_now_terminal_task_is_a_terminal_conflict() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        c.execute(
            "UPDATE tasks SET status = 'completed' WHERE task_id = 't1'",
            [],
        )
        .unwrap();
        let err = edit_comment(&c, id, "alice", "v2", false).unwrap_err();
        match err {
            EditCommentError::Terminal { status, .. } => assert_eq!(status, "completed"),
            other => panic!("expected Terminal, got {other:?}"),
        }
    }

    #[test]
    fn the_author_can_delete_their_own_comment() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        delete_comment(&c, id, "alice", false).unwrap();
        assert_eq!(get_row(&c, id).unwrap(), None);
    }

    #[test]
    fn a_non_author_non_admin_delete_is_not_found_or_forbidden_and_leaves_it_intact() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        let err = delete_comment(&c, id, "bob", false).unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
        assert!(get_row(&c, id).unwrap().is_some());
    }

    #[test]
    fn deleting_a_comment_on_a_terminal_task_is_a_terminal_conflict() {
        let c = conn_with_task("t1", "in_progress");
        let id = add_comment(&c, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z").unwrap();
        // Flip the parent task terminal AFTER the comment exists --
        // add_comment itself would be trigger-blocked on an
        // already-terminal task, and this test only cares about
        // delete_comment's own terminality check.
        c.execute(
            "UPDATE tasks SET status = 'completed' WHERE task_id = 't1'",
            [],
        )
        .unwrap();
        let err = delete_comment(&c, id, "alice", false).unwrap_err();
        assert!(matches!(err, EditCommentError::Terminal { .. }));
    }
}
