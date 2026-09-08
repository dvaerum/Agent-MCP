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
//!
//! Phase G (sea-orm migration): this is the FIRST repository rewritten
//! onto sea-orm (see the plan's own "Phase G real architectural fork"
//! note — chosen over the original pub-fn-count-based ordering once
//! the REAL transitive caller blast radius was checked: this
//! repository has exactly 3 call sites, all inside `conexus-tools::
//! task_comments_tools`'s already-async `Tool::call` bodies, with no
//! further transitive ripple — unlike `group_capability_repository`,
//! whose real blast radius reaches through `resolve_capabilities`
//! into ~50+ deliberately-sync `conexus-router` decision functions).
//! `task_status`'s read of the `tasks` table uses the raw-SQL escape
//! hatch (a `sea_orm::Statement`, not a `tasks` Entity) since a real
//! `tasks` Entity is `task_repository`'s own future rewrite's job,
//! not duplicated here for one column.

use sea_orm::{
    ActiveValue::Set, ColumnTrait, ConnectionTrait, DatabaseConnection, DbErr, EntityTrait,
    QueryFilter, Statement,
};

pub use crate::entity::task_comment::Model as TaskCommentRow;
use crate::entity::task_comment::{ActiveModel, Column, Entity};

/// The literal SQLite trigger names/checks the `task_comments`
/// terminal-guard triggers' `RAISE(ABORT, ...)` message against —
/// shared with `task_repository`'s own copy since both match the same
/// static marker embedded in `schema.rs`'s DDL (SQLite's trigger
/// grammar only accepts a literal for `RAISE`). Matched by substring
/// against the sea-orm/sqlx error's own `Display` text — verified
/// empirically (a throwaway probe against the real trigger) that the
/// marker text survives intact through `DbErr::Exec(SqlxError(...))`'s
/// wrapping, mirroring Python's `GUARD_MARKER in str(e)` check.
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
    Db(DbErr),
}

fn classify_insert_error(task_id: &str, e: DbErr) -> AddCommentError {
    let msg = e.to_string();
    if msg.contains(GUARD_MARKER) {
        return AddCommentError::TerminalTaskWriteBlocked(TerminalTaskWriteBlocked {
            task_id: task_id.to_string(),
            message: msg,
        });
    }
    AddCommentError::Db(e)
}

/// INSERT a new comment, returning its autoincrement `note_id`.
/// Matches Python's `add_comment`'s empty-text/task_id rejection —
/// callers are expected to validate before reaching this (the tool
/// layer already does), so this stays dumb CRUD.
pub async fn add_comment(
    db: &DatabaseConnection,
    task_id: &str,
    author: Option<&str>,
    text: &str,
    now: &str,
) -> Result<i64, AddCommentError> {
    let am = ActiveModel {
        task_id: Set(task_id.to_string()),
        author: Set(author.map(str::to_string)),
        timestamp: Set(now.to_string()),
        text: Set(text.to_string()),
        ..Default::default()
    };
    let inserted = Entity::insert(am)
        .exec(db)
        .await
        .map_err(|e| classify_insert_error(task_id, e))?;
    Ok(inserted.last_insert_id)
}

async fn get_row(db: &DatabaseConnection, note_id: i64) -> Result<Option<TaskCommentRow>, DbErr> {
    Entity::find_by_id(note_id).one(db).await
}

/// Raw-SQL escape hatch (see this module's own doc): reads
/// `tasks.status` for `task_id` without a `tasks` Entity, which is
/// `task_repository`'s own future Phase G rewrite to define.
async fn task_status(db: &DatabaseConnection, task_id: &str) -> Result<Option<String>, DbErr> {
    let stmt = Statement::from_sql_and_values(
        sea_orm::DatabaseBackend::Sqlite,
        "SELECT status FROM tasks WHERE task_id = ?",
        [task_id.into()],
    );
    let row = db.query_one_raw(stmt).await?;
    match row {
        Some(row) => Ok(Some(row.try_get("", "status")?)),
        None => Ok(None),
    }
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
    Db(DbErr),
}

pub type DeleteCommentError = EditCommentError;

/// Update a comment's text. Only the original author or `is_admin`
/// may edit. Ownership is checked BEFORE terminality (OBS-R12-2: a
/// non-owner/non-admin requester must get the same fused refusal
/// regardless of the task's status — checking terminality first would
/// let a non-owner distinguish "comment on a terminal task" from
/// "comment on a live task" from which error comes back, a new PF-1-
/// shaped oracle).
pub async fn edit_comment(
    db: &DatabaseConnection,
    note_id: i64,
    requester: &str,
    new_text: &str,
    is_admin: bool,
) -> Result<(), EditCommentError> {
    let row = get_row(db, note_id).await.map_err(EditCommentError::Db)?;
    let Some(row) = row else {
        return Err(EditCommentError::NotFoundOrForbidden);
    };
    if !is_admin && row.author.as_deref() != Some(requester) {
        return Err(EditCommentError::NotFoundOrForbidden);
    }
    let status = task_status(db, &row.task_id)
        .await
        .map_err(EditCommentError::Db)?;
    if let Some(status) = &status {
        if TERMINAL_STATUSES.contains(&status.as_str()) {
            return Err(EditCommentError::Terminal {
                note_id,
                task_id: row.task_id,
                status: status.clone(),
            });
        }
    }
    let update_result = Entity::update_many()
        .col_expr(Column::Text, sea_orm::sea_query::Expr::value(new_text))
        .filter(Column::NoteId.eq(note_id))
        .exec(db)
        .await;
    update_result.map_err(|e| {
        // Defense-in-depth (Python's own comment: "never reachable in
        // normal operation" since the terminality check above already
        // refused this) -- the DB trigger firing here would otherwise
        // surface as an opaque Db error.
        let msg = e.to_string();
        if msg.contains(GUARD_MARKER) {
            return EditCommentError::Terminal {
                note_id,
                task_id: row.task_id.clone(),
                status: status.clone().unwrap_or_default(),
            };
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
pub async fn delete_comment(
    db: &DatabaseConnection,
    note_id: i64,
    requester: &str,
    is_admin: bool,
) -> Result<(), DeleteCommentError> {
    let row = get_row(db, note_id).await.map_err(EditCommentError::Db)?;
    let Some(row) = row else {
        return Err(EditCommentError::NotFoundOrForbidden);
    };
    if !is_admin && row.author.as_deref() != Some(requester) {
        return Err(EditCommentError::NotFoundOrForbidden);
    }
    let status = task_status(db, &row.task_id)
        .await
        .map_err(EditCommentError::Db)?;
    if let Some(status) = status {
        if TERMINAL_STATUSES.contains(&status.as_str()) {
            return Err(EditCommentError::Terminal {
                note_id,
                task_id: row.task_id,
                status,
            });
        }
    }
    Entity::delete_by_id(note_id)
        .exec(db)
        .await
        .map_err(EditCommentError::Db)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::init_schema;
    use sea_orm::Database;

    async fn conn_with_task(
        task_id: &str,
        status: &str,
    ) -> (tempfile::TempDir, DatabaseConnection) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.db");
        {
            let c = rusqlite::Connection::open(&path).unwrap();
            init_schema(&c).unwrap();
            c.execute(
                "INSERT INTO tasks (task_id, title, created_by, status, priority, created_at, \
                 updated_at) VALUES (?1, 'Task', 'alice', ?2, 'medium', '2026-06-01T00:00:00Z', \
                 '2026-06-01T00:00:00Z')",
                (task_id, status),
            )
            .unwrap();
        }
        let url = format!("sqlite://{}", path.display());
        let db = Database::connect(&url).await.unwrap();
        (dir, db)
    }

    async fn set_task_status(db: &DatabaseConnection, task_id: &str, status: &str) {
        let stmt = Statement::from_sql_and_values(
            sea_orm::DatabaseBackend::Sqlite,
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            [status.into(), task_id.into()],
        );
        db.execute_raw(stmt).await.unwrap();
    }

    #[tokio::test]
    async fn add_comment_returns_an_incrementing_note_id() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id1 = add_comment(&db, "t1", Some("alice"), "first", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        let id2 = add_comment(&db, "t1", Some("alice"), "second", "2026-06-01T00:01:00Z")
            .await
            .unwrap();
        assert!(id2 > id1);
    }

    #[tokio::test]
    async fn add_comment_on_a_terminal_task_is_blocked_by_the_db_trigger() {
        let (_dir, db) = conn_with_task("t1", "completed").await;
        let err = add_comment(&db, "t1", Some("alice"), "too late", "2026-06-01T00:00:00Z")
            .await
            .unwrap_err();
        assert!(matches!(err, AddCommentError::TerminalTaskWriteBlocked(_)));
    }

    #[tokio::test]
    async fn the_author_can_edit_their_own_comment() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        edit_comment(&db, id, "alice", "v2", false).await.unwrap();
        let row = get_row(&db, id).await.unwrap().unwrap();
        assert_eq!(row.text, "v2");
    }

    #[tokio::test]
    async fn a_non_author_non_admin_edit_is_not_found_or_forbidden() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        let err = edit_comment(&db, id, "bob", "v2", false).await.unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
    }

    #[tokio::test]
    async fn an_admin_can_edit_someone_elses_comment() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        edit_comment(&db, id, "bob", "moderated", true)
            .await
            .unwrap();
        let row = get_row(&db, id).await.unwrap().unwrap();
        assert_eq!(row.text, "moderated");
    }

    #[tokio::test]
    async fn editing_a_nonexistent_comment_is_not_found_or_forbidden() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let err = edit_comment(&db, 999, "alice", "v2", false)
            .await
            .unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
    }

    #[tokio::test]
    async fn editing_a_comment_on_a_now_terminal_task_is_a_terminal_conflict() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        set_task_status(&db, "t1", "completed").await;
        let err = edit_comment(&db, id, "alice", "v2", false)
            .await
            .unwrap_err();
        match err {
            EditCommentError::Terminal { status, .. } => assert_eq!(status, "completed"),
            other => panic!("expected Terminal, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn the_author_can_delete_their_own_comment() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        delete_comment(&db, id, "alice", false).await.unwrap();
        assert_eq!(get_row(&db, id).await.unwrap(), None);
    }

    #[tokio::test]
    async fn a_non_author_non_admin_delete_is_not_found_or_forbidden_and_leaves_it_intact() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        let err = delete_comment(&db, id, "bob", false).await.unwrap_err();
        assert!(matches!(err, EditCommentError::NotFoundOrForbidden));
        assert!(get_row(&db, id).await.unwrap().is_some());
    }

    #[tokio::test]
    async fn deleting_a_comment_on_a_terminal_task_is_a_terminal_conflict() {
        let (_dir, db) = conn_with_task("t1", "in_progress").await;
        let id = add_comment(&db, "t1", Some("alice"), "v1", "2026-06-01T00:00:00Z")
            .await
            .unwrap();
        // Flip the parent task terminal AFTER the comment exists --
        // add_comment itself would be trigger-blocked on an
        // already-terminal task, and this test only cares about
        // delete_comment's own terminality check.
        set_task_status(&db, "t1", "completed").await;
        let err = delete_comment(&db, id, "alice", false).await.unwrap_err();
        assert!(matches!(err, EditCommentError::Terminal { .. }));
    }
}
