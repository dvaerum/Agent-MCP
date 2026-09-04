//! Port of `agent_mcp/tools/task_tools.py`'s shared task-mutation
//! engine (Phase D4, PR 5/8): `_update_single_task`,
//! `_advance_dependents_after_completion`, `_worker_ownership_deny`/
//! `_worker_ownership_deny_result`, and the post-commit wake tail
//! (`_wake_task_assignees`'s Rust equivalent, wired through
//! `ToolCallContext::waiter_registry` instead of a bare function
//! call). Consumed by `UpdateTaskStatusTool`/`UpdateTaskTool` (this
//! same PR) and, later, `bulk_task_operations` (PR 8) -- the single
//! source of truth so those surfaces cannot drift apart, matching
//! Python's own BL-R26-1 rationale for centralizing this engine.
//!
//! ## Deliberate improvement over a literal port: typed outcomes, not
//! substring-sniffed error strings
//!
//! Python's `_update_single_task` returns a `{"success": bool,
//! "error": str, ...}` dict; both callers then re-derive the right
//! `ToolResult` shape by substring-matching the error text
//! (`"not found" in err_text`, `err_text.startswith("unauthorized")`,
//! `"invalid status transition" in lower`, `"does not exist or is
//! terminated" in lower`). [`UpdateSingleTaskOutcome`] replaces that
//! with a closed enum every caller matches exhaustively -- the same
//! "compiler-enforced exhaustiveness beats an `isinstance`/substring
//! ladder" pattern `ToolResult` itself demonstrates (Phase A). This is
//! a genuine improvement, not a preserved contract: a typo'd match
//! string in Python fails silently (falls through to a generic
//! `Failed`); a missing arm here is a compile error.
//!
//! ## Deliberate deferral, documented not dropped: RAG reindexing
//!
//! Python's post-commit tail also fire-and-forgets
//! `features/rag/indexing.index_task_data` via `asyncio.create_task`
//! (`_reindex_tasks`). Porting that for real needs a genuine new seam
//! this crate doesn't have yet: `Tool::call`'s `&'a
//! AsyncMutex<Connection>` is a *borrow*, not an owned/`Arc`'d handle,
//! so nothing here can hand a detached `tokio::spawn`ed task a
//! connection that outlives the call -- Python's own version doesn't
//! reuse the caller's connection either (it opens a brand new
//! `get_db_connection()` specifically so the multi-second embedding
//! HTTP call never pins the caller's transaction). Wiring this
//! correctly is its own architectural decision (a spawn-with-its-own-
//! connection primitive, or threading the project's DB path through
//! `ToolCallContext`), not something to smuggle into an already-large
//! task-mutation PR. Tracked as a follow-up; the write path itself
//! (this PR) is unaffected -- RAG search simply lags until either that
//! follow-up lands or the existing periodic full-reindex backstop
//! (`run_rag_indexing_periodically`, not yet ported either) catches up.

use std::collections::HashMap;

use conexus_core::task_ownership::can_access_task;
use conexus_db::agent_repository::AgentRepository;
use conexus_db::scheduled_directive_repository::NullableUpdate;
use conexus_db::task_repository::{self, TaskFields, TaskNote, TaskRow};
use rusqlite::Connection;

use crate::task_tools::{agent_assignable, find_dependency_cycle, is_status_transition_allowed};

/// The 3 statuses [`update_single_task`] and its callers treat as a
/// terminal sink. Reuses the same set `task_tools.rs`'s pure helpers
/// already import (`UNASSIGNED_TASK_TERMINAL_STATUSES`), so this
/// engine can never drift from the rest of the module's terminal
/// vocabulary.
use conexus_wakeloop::event_feed::UNASSIGNED_TASK_TERMINAL_STATUSES as TERMINAL_TASK_STATUSES;

/// Port of `_worker_ownership_deny` -- the worker-facing message for a
/// task a non-admin caller doesn't own, splitting the two cases that
/// must stay distinct (see the Python docstring, reproduced here):
/// an UNASSIGNED task (no owner to hide, already public in the
/// claimable pool) gets an actionable claim-it-first message; a
/// FOREIGN-owned task gets the unchanged phantom "not found" so a
/// worker can never learn the owning agent's identity (PF-1/AZ-R17-1).
pub fn worker_ownership_deny(task_id: &str, assignee: Option<&str>, action: &str) -> String {
    match assignee {
        None => unauthorized_claim_message(task_id, action),
        Some(a) if a.trim().is_empty() => unauthorized_claim_message(task_id, action),
        Some(_) => format!("Task '{task_id}' not found"),
    }
}

fn unauthorized_claim_message(task_id: &str, action: &str) -> String {
    format!(
        "Unauthorized: task '{task_id}' is unassigned -- you must claim it before you can \
         {action}. Call assign_task(task_ids=['{task_id}']) to self-claim it -- you do NOT \
         need to supply a token; you self-claim as the authenticated caller. Then retry."
    )
}

/// Whether an assignee is the "unassigned, no owner to hide" case
/// [`worker_ownership_deny`] treats specially -- shared so a caller
/// building a `ToolResult` directly (rather than routing through
/// [`update_single_task`]) can apply the identical split.
pub fn is_unassigned_owner(assignee: Option<&str>) -> bool {
    assignee.is_none_or(|a| a.trim().is_empty())
}

/// Everything a caller may ask [`update_single_task`] to change in one
/// call. `notes_content`/admin fields are `None` for "leave
/// unchanged" -- the same optional-field shape Python's kwargs use.
#[derive(Debug, Clone, Default)]
pub struct TaskEdit<'a> {
    pub notes_content: Option<&'a str>,
    pub new_title: Option<&'a str>,
    pub new_description: Option<&'a str>,
    pub new_priority: Option<&'a str>,
    pub new_assigned_to: Option<&'a str>,
    pub new_depends_on_tasks: Option<&'a [String]>,
    /// BL-R29-1: an internal, system-driven reconcile (dependency
    /// auto-advance, child-cascade) bypasses ONLY the per-row
    /// ownership gate below, so it can touch a dependent/child owned
    /// by a different agent -- it grants NO admin field powers
    /// (title/priority/assignee stay gated on `is_admin_request`
    /// regardless of this flag).
    pub system_transition: bool,
    /// R21-F2: when transitioning INTO `completed`, block the write
    /// unless every entry in the task's effective `depends_on_tasks`
    /// is itself already `completed`. Defaults `true` to match
    /// Python's own default; system-driven non-completing reconciles
    /// (dependency auto-advance to `in_progress`, cascade to
    /// `cancelled`/`failed`) never trip this gate regardless of the
    /// flag's value, since it only ever fires on
    /// `new_status == "completed"`.
    pub validate_dependencies: bool,
}

impl<'a> TaskEdit<'a> {
    /// The common "just change status, nothing else" shape every
    /// system-driven reconcile in this module uses.
    pub fn status_only(notes_content: Option<&'a str>) -> Self {
        TaskEdit {
            notes_content,
            system_transition: true,
            validate_dependencies: true,
            ..Default::default()
        }
    }
}

/// Successful-path data [`update_single_task`] returns -- the diff a
/// caller needs to log/cascade/wake against. Port of the `dict` shape
/// `_update_single_task` returns on `success: True`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskUpdateApplied {
    pub task_id: String,
    pub old_status: String,
    pub new_status: String,
    pub child_tasks: Vec<String>,
    pub depends_on_tasks: Vec<String>,
}

/// Every non-DB-error outcome [`update_single_task`] can produce. See
/// this module's own doc for why this replaces Python's substring-
/// sniffed error-string routing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpdateSingleTaskOutcome {
    Applied(TaskUpdateApplied),
    /// The task row doesn't exist (or -- for a non-admin caller -- is
    /// foreign-owned, the PF-1 phantom-404 case; the two are
    /// deliberately indistinguishable to the caller).
    NotFound,
    /// The caller doesn't own an UNASSIGNED task -- carries the
    /// actionable claim-it-first guidance message.
    Unauthorized(String),
    InvalidTransition(String),
    AssigneeInvalid(String),
    DependencyCycle(String),
    DependencyIncomplete(String),
}

/// Port of `_update_single_task`. Runs entirely on the caller's own
/// open transaction (`conn`); the caller commits (or rolls back on any
/// `Err`) once every task in a bulk/cascade sweep has been processed.
/// `now` is this crate's usual explicit-clock-injection convention --
/// Python reads a live `datetime.now()` here (the one spot in this
/// function that isn't already parameterized on the caller's clock);
/// every mutating tool in this crate already threads `now` in from
/// `Tool::call`'s own signature, so this is that same convention
/// extended one level deeper, not a new pattern.
#[allow(clippy::too_many_arguments)]
pub fn update_single_task(
    conn: &Connection,
    task_id: &str,
    new_status: &str,
    requesting_agent_id: &str,
    is_admin_request: bool,
    edit: &TaskEdit,
    now: &str,
) -> rusqlite::Result<UpdateSingleTaskOutcome> {
    let Some(row) = task_repository::get_by_id(conn, task_id)? else {
        return Ok(UpdateSingleTaskOutcome::NotFound);
    };

    // SECURITY (PF-1): see worker_ownership_deny's own doc for the
    // unassigned-vs-foreign split this implements.
    if !edit.system_transition
        && !can_access_task(
            row.assigned_to.as_deref(),
            Some(row.created_by.as_str()),
            Some(requesting_agent_id),
            is_admin_request,
            false,
            false,
            false,
        )
    {
        return Ok(if is_unassigned_owner(row.assigned_to.as_deref()) {
            UpdateSingleTaskOutcome::Unauthorized(worker_ownership_deny(
                task_id,
                row.assigned_to.as_deref(),
                "update it",
            ))
        } else {
            UpdateSingleTaskOutcome::NotFound
        });
    }

    let old_status = row.status.clone();
    if !is_status_transition_allowed(Some(&old_status), new_status) {
        let message = if TERMINAL_TASK_STATUSES.contains(&old_status.as_str()) {
            format!(
                "Invalid status transition for task '{task_id}': '{old_status}' -> \
                 '{new_status}' is not allowed ({old_status} is a terminal state)."
            )
        } else {
            format!(
                "Invalid status transition for task '{task_id}': '{old_status}' -> \
                 '{new_status}' is not allowed."
            )
        };
        return Ok(UpdateSingleTaskOutcome::InvalidTransition(message));
    }

    if is_admin_request {
        if let Some(assignee) = edit.new_assigned_to {
            if !agent_assignable(conn, assignee) {
                return Ok(UpdateSingleTaskOutcome::AssigneeInvalid(format!(
                    "Cannot reassign task '{task_id}' to '{assignee}': agent does not exist \
                     or is terminated."
                )));
            }
        }
    }

    if is_admin_request {
        if let Some(deps) = edit.new_depends_on_tasks {
            if let Some(cycle) = find_dependency_cycle(conn, task_id, deps)? {
                return Ok(UpdateSingleTaskOutcome::DependencyCycle(format!(
                    "Cannot update depends_on_tasks for task '{task_id}': would introduce a \
                     dependency cycle ({}).",
                    cycle.join(" -> ")
                )));
            }
        }
    }

    if new_status == "completed" && edit.validate_dependencies {
        let effective_deps: &[String] = if is_admin_request {
            edit.new_depends_on_tasks
                .unwrap_or_else(|| row.depends_on_tasks.as_deref().unwrap_or(&[]))
        } else {
            row.depends_on_tasks.as_deref().unwrap_or(&[])
        };
        for dep_id in effective_deps {
            let dep_status = task_repository::get_by_id(conn, dep_id)?.map(|d| d.status);
            if dep_status.as_deref() != Some("completed") {
                return Ok(UpdateSingleTaskOutcome::DependencyIncomplete(format!(
                    "Cannot complete task '{task_id}': dependency '{dep_id}' is not yet \
                     completed (status: {}).",
                    dep_status.as_deref().unwrap_or("missing")
                )));
            }
        }
    }

    // Notes are append-only.
    let mut notes = row.notes.clone().unwrap_or_default();
    if let Some(content) = edit.notes_content {
        if !content.is_empty() {
            notes.push(TaskNote {
                timestamp: now.to_string(),
                author: Some(requesting_agent_id.to_string()),
                content: content.to_string(),
            });
        }
    }

    let mut fields = TaskFields {
        status: Some(new_status),
        notes: NullableUpdate::Set(notes),
        ..Default::default()
    };
    if is_admin_request {
        if let Some(title) = edit.new_title {
            fields.title = Some(title);
        }
        if let Some(description) = edit.new_description {
            fields.description = NullableUpdate::Set(description.to_string());
        }
        if let Some(priority) = edit.new_priority {
            fields.priority = Some(priority);
        }
        if let Some(assignee) = edit.new_assigned_to {
            fields.assigned_to = NullableUpdate::Set(assignee.to_string());
        }
        if let Some(deps) = edit.new_depends_on_tasks {
            fields.depends_on_tasks = NullableUpdate::Set(deps.to_vec());
        }
    }

    match task_repository::update_fields(conn, task_id, &fields, now) {
        Ok(_) => {}
        Err(task_repository::UpdateTaskError::TerminalTaskWriteBlocked(_)) => {
            // OBS-R12-2 defense-in-depth: the transition check above
            // should already have refused this. Same message shape.
            return Ok(UpdateSingleTaskOutcome::InvalidTransition(format!(
                "Invalid status transition for task '{task_id}': write refused -- task is in \
                 a terminal state (completed/cancelled/failed) and is frozen (DB-level guard)."
            )));
        }
        Err(task_repository::UpdateTaskError::Db(e)) => return Err(e),
    }

    // BL-R30-1: reconcile agents.current_task on a REBIND, using the
    // PRIOR assignee captured before the write above. Run before the
    // terminal-clear sweep so a terminal reassign composes correctly.
    if is_admin_request {
        if let Some(assignee) = edit.new_assigned_to {
            AgentRepository::reconcile_current_task_on_reassign(
                conn,
                task_id,
                row.assigned_to.as_deref(),
                Some(assignee),
                now,
            )?;
        }
    }

    if TERMINAL_TASK_STATUSES.contains(&new_status) {
        AgentRepository::clear_current_task_for(conn, task_id, now)?;
    }

    // Parent notification: an FYI note on the parent when a child
    // reaches a terminal status. Skipped (not an error) when the
    // parent is itself already terminal -- its notes are frozen by
    // the same DB-level guard trigger, and this is best-effort
    // bookkeeping the child's own legitimate completion must never be
    // blocked on.
    if TERMINAL_TASK_STATUSES.contains(&new_status) {
        if let Some(parent_id) = &row.parent_task {
            if let Some(parent) = task_repository::get_by_id(conn, parent_id)? {
                if !TERMINAL_TASK_STATUSES.contains(&parent.status.as_str()) {
                    let mut parent_notes = parent.notes.unwrap_or_default();
                    parent_notes.push(TaskNote {
                        timestamp: now.to_string(),
                        author: Some("system".to_string()),
                        content: format!(
                            "Subtask '{task_id}' ({}) status changed to: {new_status}",
                            row.title
                        ),
                    });
                    let parent_fields = TaskFields {
                        notes: NullableUpdate::Set(parent_notes),
                        ..Default::default()
                    };
                    match task_repository::update_fields(conn, parent_id, &parent_fields, now) {
                        Ok(_)
                        | Err(task_repository::UpdateTaskError::TerminalTaskWriteBlocked(_)) => {}
                        Err(task_repository::UpdateTaskError::Db(e)) => return Err(e),
                    }
                }
            }
        }
    }

    Ok(UpdateSingleTaskOutcome::Applied(TaskUpdateApplied {
        task_id: task_id.to_string(),
        old_status,
        new_status: new_status.to_string(),
        child_tasks: row.child_tasks.unwrap_or_default(),
        depends_on_tasks: row.depends_on_tasks.unwrap_or_default(),
    }))
}

/// Port of `_advance_dependents_after_completion` (Phase-3 dependency
/// advance for one just-completed task): finds every task depending
/// on `completed_task_id`; when ALL of a dependent's OTHER
/// dependencies are already `completed` and the dependent is still
/// `pending`, advances it to `in_progress` via [`update_single_task`]
/// (system-driven, crossing agent ownership per BL-R29-1). Shared by
/// the single-task and (eventually, PR 8) bulk status paths.
pub fn advance_dependents_after_completion(
    conn: &Connection,
    completed_task_id: &str,
    requesting_agent_id: &str,
    is_admin_request: bool,
    now: &str,
) -> rusqlite::Result<Vec<TaskUpdateApplied>> {
    let mut advanced = Vec::new();
    let all_tasks = task_repository::list_all(conn, None)?;
    let by_id: HashMap<&str, &TaskRow> =
        all_tasks.iter().map(|t| (t.task_id.as_str(), t)).collect();

    for task in &all_tasks {
        let deps = task.depends_on_tasks.as_deref().unwrap_or(&[]);
        if !deps.iter().any(|d| d == completed_task_id) {
            continue;
        }
        let all_deps_completed = deps.iter().all(|dep_id| {
            dep_id == completed_task_id
                || by_id
                    .get(dep_id.as_str())
                    .is_some_and(|d| d.status == "completed")
        });
        if !all_deps_completed {
            continue;
        }
        if task.status != "pending" {
            continue;
        }
        let outcome = update_single_task(
            conn,
            &task.task_id,
            "in_progress",
            requesting_agent_id,
            is_admin_request,
            &TaskEdit::status_only(Some("Auto-advanced: all dependencies completed")),
            now,
        )?;
        if let UpdateSingleTaskOutcome::Applied(applied) = outcome {
            advanced.push(applied);
        }
    }
    Ok(advanced)
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::NewTask;

    const NOW: &str = "2026-02-01T00:00:00Z";

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn seed_agent(conn: &Connection, agent_id: &str) {
        AgentRepository::create(
            conn,
            NewAgent {
                token: &format!("tok-{agent_id}"),
                agent_id,
                created_at: NOW,
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
    }

    #[allow(clippy::too_many_arguments)]
    fn seed_task(
        conn: &Connection,
        id: &str,
        status: &str,
        assigned_to: Option<&str>,
        created_by: &str,
        parent: Option<&str>,
        deps: Option<&[String]>,
    ) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by,
                status,
                priority: "medium",
                parent_task: parent,
                child_tasks: None,
                depends_on_tasks: deps,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    fn plain_edit() -> TaskEdit<'static> {
        TaskEdit {
            validate_dependencies: true,
            ..Default::default()
        }
    }

    #[test]
    fn worker_ownership_deny_unassigned_gets_claim_it_first_guidance() {
        let msg = worker_ownership_deny("t1", None, "update it");
        assert!(msg.starts_with("Unauthorized:"));
        assert!(msg.contains("assign_task"));
    }

    #[test]
    fn worker_ownership_deny_foreign_gets_the_phantom_not_found() {
        let msg = worker_ownership_deny("t1", Some("carol"), "update it");
        assert_eq!(msg, "Task 't1' not found");
    }

    #[test]
    fn update_single_task_missing_task_is_not_found() {
        let conn = test_conn();
        let outcome = update_single_task(
            &conn,
            "ghost",
            "in_progress",
            "bob",
            false,
            &plain_edit(),
            NOW,
        )
        .unwrap();
        assert_eq!(outcome, UpdateSingleTaskOutcome::NotFound);
    }

    #[test]
    fn update_single_task_non_admin_on_unassigned_task_is_unauthorized() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", None, "alice", None, None);
        let outcome =
            update_single_task(&conn, "t1", "in_progress", "bob", false, &plain_edit(), NOW)
                .unwrap();
        assert!(matches!(outcome, UpdateSingleTaskOutcome::Unauthorized(_)));
    }

    #[test]
    fn update_single_task_non_admin_on_foreign_task_is_phantom_not_found() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", Some("carol"), "alice", None, None);
        let outcome =
            update_single_task(&conn, "t1", "in_progress", "bob", false, &plain_edit(), NOW)
                .unwrap();
        assert_eq!(outcome, UpdateSingleTaskOutcome::NotFound);
    }

    #[test]
    fn update_single_task_non_admin_on_own_task_succeeds() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", Some("bob"), "alice", None, None);
        let outcome =
            update_single_task(&conn, "t1", "in_progress", "bob", false, &plain_edit(), NOW)
                .unwrap();
        assert!(matches!(outcome, UpdateSingleTaskOutcome::Applied(_)));
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        assert_eq!(row.status, "in_progress");
    }

    #[test]
    fn update_single_task_rejects_a_transition_out_of_a_terminal_state() {
        let conn = test_conn();
        seed_task(&conn, "t1", "completed", Some("bob"), "alice", None, None);
        let outcome =
            update_single_task(&conn, "t1", "in_progress", "bob", true, &plain_edit(), NOW)
                .unwrap();
        assert!(matches!(
            outcome,
            UpdateSingleTaskOutcome::InvalidTransition(_)
        ));
    }

    #[test]
    fn update_single_task_admin_reassign_to_a_dead_agent_is_invalid() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", None, "alice", None, None);
        let edit = TaskEdit {
            new_assigned_to: Some("ghost-agent"),
            validate_dependencies: true,
            ..Default::default()
        };
        let outcome =
            update_single_task(&conn, "t1", "pending", "alice", true, &edit, NOW).unwrap();
        assert!(matches!(
            outcome,
            UpdateSingleTaskOutcome::AssigneeInvalid(_)
        ));
    }

    #[test]
    fn update_single_task_admin_dependency_cycle_is_rejected() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", None, "alice", None, None);
        seed_task(&conn, "t2", "pending", None, "alice", Some("t1"), None);
        // t2 already depends on t1; wiring t1 -> t2 would cycle.
        task_repository::update_fields(
            &conn,
            "t2",
            &TaskFields {
                depends_on_tasks: NullableUpdate::Set(vec!["t1".to_string()]),
                ..Default::default()
            },
            NOW,
        )
        .unwrap();
        let deps = vec!["t2".to_string()];
        let edit = TaskEdit {
            new_depends_on_tasks: Some(&deps),
            validate_dependencies: true,
            ..Default::default()
        };
        let outcome =
            update_single_task(&conn, "t1", "pending", "alice", true, &edit, NOW).unwrap();
        assert!(matches!(
            outcome,
            UpdateSingleTaskOutcome::DependencyCycle(_)
        ));
    }

    #[test]
    fn update_single_task_completing_with_an_incomplete_dependency_is_blocked() {
        let conn = test_conn();
        seed_task(&conn, "dep", "pending", None, "alice", None, None);
        seed_task(
            &conn,
            "t1",
            "in_progress",
            None,
            "alice",
            Some("dep"),
            Some(&["dep".to_string()]),
        );
        let outcome =
            update_single_task(&conn, "t1", "completed", "alice", true, &plain_edit(), NOW)
                .unwrap();
        assert!(matches!(
            outcome,
            UpdateSingleTaskOutcome::DependencyIncomplete(_)
        ));
    }

    #[test]
    fn update_single_task_completing_with_a_completed_dependency_succeeds() {
        let conn = test_conn();
        seed_task(&conn, "dep", "completed", None, "alice", None, None);
        seed_task(
            &conn,
            "t1",
            "in_progress",
            None,
            "alice",
            Some("dep"),
            Some(&["dep".to_string()]),
        );
        let outcome =
            update_single_task(&conn, "t1", "completed", "alice", true, &plain_edit(), NOW)
                .unwrap();
        assert!(matches!(outcome, UpdateSingleTaskOutcome::Applied(_)));
    }

    #[test]
    fn update_single_task_appends_a_note() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", Some("bob"), "alice", None, None);
        let edit = TaskEdit {
            notes_content: Some("progress update"),
            validate_dependencies: true,
            ..Default::default()
        };
        update_single_task(&conn, "t1", "in_progress", "bob", false, &edit, NOW).unwrap();
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        let notes = row.notes.unwrap();
        assert_eq!(notes.len(), 1);
        assert_eq!(notes[0].content, "progress update");
        assert_eq!(notes[0].author.as_deref(), Some("bob"));
    }

    #[test]
    fn update_single_task_admin_fields_are_ignored_for_a_non_admin_caller() {
        let conn = test_conn();
        seed_task(&conn, "t1", "pending", Some("bob"), "alice", None, None);
        let edit = TaskEdit {
            new_title: Some("hijacked title"),
            validate_dependencies: true,
            ..Default::default()
        };
        update_single_task(&conn, "t1", "in_progress", "bob", false, &edit, NOW).unwrap();
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        assert_eq!(row.title, "Task t1");
    }

    #[test]
    fn update_single_task_admin_reassign_reconciles_current_task() {
        let conn = test_conn();
        seed_agent(&conn, "carol");
        seed_task(&conn, "t1", "pending", None, "alice", None, None);
        let edit = TaskEdit {
            new_assigned_to: Some("carol"),
            validate_dependencies: true,
            ..Default::default()
        };
        update_single_task(&conn, "t1", "pending", "alice", true, &edit, NOW).unwrap();
        let agent = AgentRepository::get_by_id(&conn, "carol").unwrap().unwrap();
        assert_eq!(agent.current_task.as_deref(), Some("t1"));
    }

    #[test]
    fn update_single_task_terminal_status_clears_the_assignees_current_task() {
        let conn = test_conn();
        seed_agent(&conn, "bob");
        AgentRepository::reconcile_current_task_on_reassign(&conn, "t1", None, Some("bob"), NOW)
            .unwrap();
        seed_task(&conn, "t1", "in_progress", Some("bob"), "alice", None, None);
        // seed_task runs after the reconcile above (task_id FK doesn't
        // exist yet at reconcile time) -- reconcile again now that the
        // row is real, matching the shape a live create_task+assign
        // sequence would produce.
        AgentRepository::reconcile_current_task_on_reassign(&conn, "t1", None, Some("bob"), NOW)
            .unwrap();
        update_single_task(&conn, "t1", "completed", "bob", false, &plain_edit(), NOW).unwrap();
        let agent = AgentRepository::get_by_id(&conn, "bob").unwrap().unwrap();
        assert_eq!(agent.current_task, None);
    }

    #[test]
    fn update_single_task_notifies_a_non_terminal_parent_on_child_completion() {
        let conn = test_conn();
        seed_task(&conn, "root1", "pending", None, "alice", None, None);
        seed_task(
            &conn,
            "child1",
            "in_progress",
            None,
            "alice",
            Some("root1"),
            None,
        );
        update_single_task(
            &conn,
            "child1",
            "completed",
            "alice",
            true,
            &plain_edit(),
            NOW,
        )
        .unwrap();
        let parent = task_repository::get_by_id(&conn, "root1").unwrap().unwrap();
        let notes = parent.notes.unwrap();
        assert_eq!(notes.len(), 1);
        assert!(notes[0].content.contains("child1"));
        assert_eq!(notes[0].author.as_deref(), Some("system"));
    }

    #[test]
    fn update_single_task_skips_notifying_an_already_terminal_parent() {
        let conn = test_conn();
        seed_task(&conn, "root1", "completed", None, "alice", None, None);
        seed_task(
            &conn,
            "child1",
            "in_progress",
            None,
            "alice",
            Some("root1"),
            None,
        );
        let outcome = update_single_task(
            &conn,
            "child1",
            "completed",
            "alice",
            true,
            &plain_edit(),
            NOW,
        )
        .unwrap();
        assert!(matches!(outcome, UpdateSingleTaskOutcome::Applied(_)));
        let parent = task_repository::get_by_id(&conn, "root1").unwrap().unwrap();
        assert_eq!(parent.notes, None);
    }

    #[test]
    fn advance_dependents_after_completion_advances_a_now_unblocked_dependent() {
        let conn = test_conn();
        seed_task(&conn, "dep", "completed", None, "alice", None, None);
        seed_task(
            &conn,
            "dependent",
            "pending",
            None,
            "alice",
            Some("dep"),
            Some(&["dep".to_string()]),
        );
        let advanced =
            advance_dependents_after_completion(&conn, "dep", "alice", true, NOW).unwrap();
        assert_eq!(advanced.len(), 1);
        assert_eq!(advanced[0].task_id, "dependent");
        let row = task_repository::get_by_id(&conn, "dependent")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "in_progress");
    }

    #[test]
    fn advance_dependents_after_completion_waits_for_every_dependency() {
        let conn = test_conn();
        seed_task(&conn, "dep_a", "completed", None, "alice", None, None);
        seed_task(
            &conn,
            "dep_b",
            "pending",
            None,
            "alice",
            Some("dep_a"),
            None,
        );
        seed_task(
            &conn,
            "dependent",
            "pending",
            None,
            "alice",
            Some("dep_a"),
            Some(&["dep_a".to_string(), "dep_b".to_string()]),
        );
        let advanced =
            advance_dependents_after_completion(&conn, "dep_a", "alice", true, NOW).unwrap();
        assert!(advanced.is_empty());
        let row = task_repository::get_by_id(&conn, "dependent")
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "pending");
    }

    #[test]
    fn advance_dependents_after_completion_ignores_a_non_pending_dependent() {
        let conn = test_conn();
        seed_task(&conn, "dep", "completed", None, "alice", None, None);
        seed_task(
            &conn,
            "dependent",
            "in_progress",
            None,
            "alice",
            Some("dep"),
            Some(&["dep".to_string()]),
        );
        let advanced =
            advance_dependents_after_completion(&conn, "dep", "alice", true, NOW).unwrap();
        assert!(advanced.is_empty());
    }
}
