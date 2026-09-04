//! Port of `agent_mcp/tools/task_comments_tools.py` (Phase D5, PR 6):
//! the `task_comments` side table's three MCP tools — `add_task_comment`,
//! `edit_task_comment`, `delete_task_comment`. Replaces the legacy
//! `tasks.notes` JSON-list-in-TEXT pattern with per-comment
//! edit/delete (`conexus_db::task_comments_repository`, Phase D5's new
//! DB scope for this PR).
//!
//! `Requirement::Predicate` on all three (matching Python's own
//! Finding-A rationale): admission is `AgentBearer` OR operator-tier
//! (`system.config.write`) — a compound `kind`-OR-capability rule no
//! single `Cap` reproduces.
//!
//! Ownership model, ported faithfully:
//! - `add_task_comment`: a per-TASK ownership gate
//!   (`conexus_core::task_ownership::can_access_task`) — the task must
//!   exist, and the caller must be its assignee/creator, manager-tier
//!   (`tasks.assign`), or — when `config_allow_worker_comment_foreign_tasks`
//!   is on (default `true`) — any worker on any ASSIGNED task. An
//!   UNASSIGNED task always needs claiming first regardless of that
//!   toggle (SEC PF-1: a foreign-owned task collapses to the same
//!   phantom `NotFound` a nonexistent task returns; an unassigned task
//!   gets the actionable claim-it-first `PermissionDenied` instead —
//!   reuses `task_mutation_engine::{is_unassigned_owner,
//!   worker_ownership_deny}`, the exact same split every other
//!   ownership-gated tool this migration has ported already uses).
//! - `edit_task_comment`/`delete_task_comment`: a per-COMMENT
//!   authorship gate inside `task_comments_repository` itself —
//!   `EditCommentError::NotFoundOrForbidden` is the SAME response
//!   whether the comment doesn't exist or exists but belongs to
//!   someone else (SEC PF-1's comment-existence-oracle fusion,
//!   structural in the repository's own type rather than a
//!   string-matched fuse at the tool layer the way Python's
//!   `_classify_db_error` needs to be).
//!
//! Deliberately NOT ported, with an explicit reason (never a silent
//! drop): the in-memory `log_audit` trail — same precedent as every
//! prior Phase D5 tool.

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::task_ownership::can_access_task;
use conexus_core::tool_result::ToolResult;
use conexus_db::task_comments_repository::{self, AddCommentError, EditCommentError};
use conexus_db::{project_settings_repository, task_repository};
use rusqlite::Connection;
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::task_mutation_engine::{is_unassigned_owner, worker_ownership_deny};
use crate::task_tools::str_arg;

const TERMINAL_STATUSES: &[&str] = &["completed", "cancelled", "failed"];

fn is_agent_or_operator_caller(principal: Option<&Principal>) -> bool {
    principal.is_some_and(|p| {
        p.kind == conexus_core::principal::PrincipalKind::AgentBearer
            || p.has_capability(Capability::SystemConfigWrite)
    })
}

const ADD_DENIED: &str = "Unauthorized: agent or operator token required to add a task comment";
const EDIT_DENIED: &str = "Unauthorized: agent or operator token required to edit a task comment";
const DELETE_DENIED: &str =
    "Unauthorized: agent or operator token required to delete a task comment";

/// One fused, static hint added to the `NotFound` both the
/// missing-comment and foreign-comment outcomes return — see this
/// module's doc / `task_comments_repository::EditCommentError`'s own
/// doc on why no author identity is ever interpolated here.
const AUTHOR_ONLY_HINT: &str = ", or you are not its author. Only a comment's original author \
    (or an admin) can edit or delete it.";

fn note_id_arg(arguments: &Value) -> Result<i64, ToolResult> {
    let Some(raw) = arguments.get("note_id") else {
        return Err(ToolResult::Invalid {
            field: Some("note_id".to_string()),
            message: "`note_id` is required.".to_string(),
        });
    };
    let parsed = match raw {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.parse::<i64>().ok(),
        _ => None,
    };
    parsed.ok_or_else(|| ToolResult::Invalid {
        field: Some("note_id".to_string()),
        message: format!("`note_id` must be an integer, got {raw:?}."),
    })
}

fn edit_or_delete_error_to_tool_result(note_id: i64, err: EditCommentError) -> ToolResult {
    match err {
        EditCommentError::NotFoundOrForbidden => ToolResult::NotFound {
            resource: "task comment".to_string(),
            identifier: note_id.to_string(),
            hint: Some(AUTHOR_ONLY_HINT.to_string()),
        },
        EditCommentError::Terminal {
            note_id,
            task_id,
            status,
        } => ToolResult::Conflict {
            reason: format!(
                "Comment {note_id}'s task '{task_id}' is in a terminal state ({status}); its \
                 comments are frozen."
            ),
        },
        EditCommentError::Db(_e) => ToolResult::Failed {
            message: "A database error occurred; it has been logged. Retry, or ask an operator \
                to check logs."
                .to_string(),
        },
    }
}

pub struct AddTaskCommentTool;

impl Tool for AddTaskCommentTool {
    const NAME: &'static str = "add_task_comment";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_agent_or_operator_caller,
        reason: ADD_DENIED,
    };
    const DESCRIPTION: &'static str = "Add a comment to a task via the side table (db-review \
        PR-H). Returns the new note_id. Comments added this way can be edited/deleted by the \
        original author or admin. By default you may comment on any ASSIGNED task -- your own \
        or another agent's (project policy may restrict this to tasks you own/created). An \
        UNASSIGNED task (in the claimable pool) always needs claiming first, regardless of that \
        policy -- call assign_task(task_ids=[...], agent_token=<your own>).";
    // "maxLength": 256 mirrors conexus_core::schema_limits::IDENTIFIER_MAX_LEN
    // -- kept as a literal (SCHEMA must be `const`-constructible) and
    // cross-checked against that constant by this module's own test.
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task to attach the comment to.",
                "maxLength": 256
            },
            "text": {
                "type": "string",
                "description": "Comment text."
            }
        },
        "required": ["task_id", "text"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let Some(task_id) = str_arg(arguments, "task_id").filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("task_id".to_string()),
                    message: "`task_id` is required.".to_string(),
                };
            };
            let Some(text) = str_arg(arguments, "text").filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("text".to_string()),
                    message: "`text` is required.".to_string(),
                };
            };

            let guard = conn.lock().await;
            let task = match task_repository::get_by_id(&guard, &task_id) {
                Ok(Some(t)) => t,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: task_id,
                        hint: None,
                    }
                }
                Err(_e) => {
                    return ToolResult::Failed {
                        message: "A database error occurred; it has been logged. Retry, or ask \
                            an operator to check logs."
                            .to_string(),
                    }
                }
            };

            let requester = principal
                .and_then(|p| p.agent_id.clone().or_else(|| p.user_id.clone()))
                .unwrap_or_default();
            let can_view_all = principal.is_some_and(|p| p.has_capability(Capability::TasksAssign));
            let include_foreign = project_settings_repository::get_bool(
                &guard,
                "config_allow_worker_comment_foreign_tasks",
                true,
            );

            if !can_access_task(
                task.assigned_to.as_deref(),
                Some(&task.created_by),
                Some(&requester),
                can_view_all,
                true,
                false,
                include_foreign,
            ) {
                return if is_unassigned_owner(task.assigned_to.as_deref()) {
                    ToolResult::PermissionDenied {
                        reason: worker_ownership_deny(
                            &task_id,
                            task.assigned_to.as_deref(),
                            "add a comment to it",
                        )
                        .strip_prefix("Unauthorized: ")
                        .unwrap_or_default()
                        .to_string(),
                    }
                } else {
                    ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: task_id,
                        hint: None,
                    }
                };
            }

            // OBS-R12-2 (round-13 class-sweep): checked AFTER the
            // ownership gate above so a non-owner probing a foreign
            // task's comments still gets the SAME fused not-found/
            // denied result regardless of that task's status.
            if TERMINAL_STATUSES.contains(&task.status.as_str()) {
                return ToolResult::Conflict {
                    reason: format!(
                        "Cannot add a comment to task '{task_id}': status '{}' is terminal \
                         (completed/cancelled/failed) and its comments are frozen.",
                        task.status
                    ),
                };
            }

            let author = principal.and_then(|p| p.agent_id.clone().or_else(|| p.user_id.clone()));
            match task_comments_repository::add_comment(
                &guard,
                &task_id,
                author.as_deref(),
                &text,
                now,
            ) {
                Ok(note_id) => ToolResult::Ok {
                    data: Some(serde_json::json!({"note_id": note_id, "task_id": task_id})),
                    message: Some(format!("Comment {note_id} added to task '{task_id}'.")),
                },
                Err(AddCommentError::TerminalTaskWriteBlocked(_e)) => {
                    // Defense-in-depth: the check above should already
                    // have refused this. Never reachable in normal
                    // operation.
                    ToolResult::Conflict {
                        reason: format!(
                            "Cannot add a comment to task '{task_id}': it is in a terminal state."
                        ),
                    }
                }
                Err(AddCommentError::Db(_e)) => ToolResult::Failed {
                    message: format!("Failed to add comment to task '{task_id}'."),
                },
            }
        })
    }
}

pub struct EditTaskCommentTool;

impl Tool for EditTaskCommentTool {
    const NAME: &'static str = "edit_task_comment";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_agent_or_operator_caller,
        reason: EDIT_DENIED,
    };
    const DESCRIPTION: &'static str =
        "Edit a task comment (db-review PR-H side table). Only the original author or admin \
         may edit.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "note_id": {
                "type": "integer",
                "description": "Side-table note_id to edit.",
                "minimum": 1,
                "maximum": 9223372036854775807
            },
            "text": {
                "type": "string",
                "description": "Replacement comment text."
            }
        },
        "required": ["note_id", "text"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let note_id = match note_id_arg(arguments) {
                Ok(n) => n,
                Err(e) => return e,
            };
            let Some(new_text) = str_arg(arguments, "text").filter(|s| !s.is_empty()) else {
                return ToolResult::Invalid {
                    field: Some("text".to_string()),
                    message: "`text` is required.".to_string(),
                };
            };

            let requester = principal
                .and_then(|p| p.agent_id.clone().or_else(|| p.user_id.clone()))
                .unwrap_or_default();
            let is_admin = principal.is_some_and(|p| p.has_capability(Capability::TasksAssign));

            let guard = conn.lock().await;
            match task_comments_repository::edit_comment(
                &guard, note_id, &requester, &new_text, is_admin,
            ) {
                Ok(()) => ToolResult::Ok {
                    data: Some(serde_json::json!({"note_id": note_id})),
                    message: Some(format!("Comment {note_id} updated.")),
                },
                Err(e) => edit_or_delete_error_to_tool_result(note_id, e),
            }
        })
    }
}

pub struct DeleteTaskCommentTool;

impl Tool for DeleteTaskCommentTool {
    const NAME: &'static str = "delete_task_comment";
    const REQUIRED: Requirement = Requirement::Predicate {
        check: is_agent_or_operator_caller,
        reason: DELETE_DENIED,
    };
    const DESCRIPTION: &'static str = "Delete a task comment (db-review PR-H side table). Only \
        the original author or admin may delete.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "note_id": {
                "type": "integer",
                "description": "Side-table note_id to delete.",
                "minimum": 1,
                "maximum": 9223372036854775807
            }
        },
        "required": ["note_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        _now: &'a str,
        _ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let note_id = match note_id_arg(arguments) {
                Ok(n) => n,
                Err(e) => return e,
            };

            let requester = principal
                .and_then(|p| p.agent_id.clone().or_else(|| p.user_id.clone()))
                .unwrap_or_default();
            let is_admin = principal.is_some_and(|p| p.has_capability(Capability::TasksAssign));

            let guard = conn.lock().await;
            match task_comments_repository::delete_comment(&guard, note_id, &requester, is_admin) {
                Ok(()) => ToolResult::Ok {
                    data: Some(serde_json::json!({"note_id": note_id})),
                    message: Some(format!("Comment {note_id} deleted.")),
                },
                Err(e) => edit_or_delete_error_to_tool_result(note_id, e),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_auth::ToolCallContext;
    use conexus_core::capability::{AgentRole, Capabilities};
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::file_map::FileMap;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;
    use std::collections::HashSet;

    fn worker(agent_id: &str, caps: &[Capability]) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: Some(AgentRole::Worker),
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::Set(caps.iter().copied().collect::<HashSet<_>>()),
        }
    }

    fn operator() -> Principal {
        Principal {
            kind: PrincipalKind::ForwardingHeader,
            user_id: Some("op-1".to_string()),
            agent_id: None,
            project_name: Some("demo".to_string()),
            project_role: Some(conexus_core::capability::ProjectRole::Operator),
            agent_role: None,
            can_wake_loop: false,
            source_token: None,
            // A real operator principal carries the whole
            // PROJECT_ROLE_BUNDLES["operator"] set, which includes
            // `tasks.assign` (the manager-tier marker `is_admin`
            // reads) alongside `system.config.write` (the admission
            // gate this module's predicate checks) -- both included
            // here to match.
            capabilities: Capabilities::Set(HashSet::from([
                Capability::SystemConfigWrite,
                Capability::TasksAssign,
            ])),
        }
    }

    async fn setup() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn seed_task(conn: &Connection, task_id: &str, assigned_to: Option<&str>, created_by: &str) {
        conn.execute(
            "INSERT INTO tasks (task_id, title, created_by, assigned_to, status, priority, \
             created_at, updated_at) VALUES (?1, 'T', ?2, ?3, 'in_progress', 'medium', \
             '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')",
            (task_id, created_by, assigned_to),
        )
        .unwrap();
    }

    fn ctx<'a>(registry: &'a WaiterRegistry, file_map: &'a FileMap) -> ToolCallContext<'a> {
        ToolCallContext::off_wire(registry, file_map, std::path::Path::new("/tmp"))
    }

    #[tokio::test]
    async fn a_worker_can_comment_on_their_own_assigned_task() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", Some("alice"), "alice");
        }
        let alice = worker("alice", &[Capability::TasksCreate]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "hello"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
    }

    #[tokio::test]
    async fn commenting_on_a_nonexistent_task_is_not_found() {
        let conn = setup().await;
        let alice = worker("alice", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "ghost", "text": "hi"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn commenting_on_an_unassigned_task_is_permission_denied_with_claim_guidance() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", None, "bob");
        }
        let alice = worker("alice", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "hi"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::PermissionDenied { reason } = result else {
            panic!("expected PermissionDenied, got {result:?}");
        };
        assert!(reason.contains("claim"));
    }

    #[tokio::test]
    async fn commenting_on_a_terminal_task_is_conflict() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", Some("alice"), "alice");
            c.execute(
                "UPDATE tasks SET status = 'completed' WHERE task_id = 't1'",
                [],
            )
            .unwrap();
        }
        let alice = worker("alice", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "hi"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn the_author_can_edit_their_own_comment() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", Some("alice"), "alice");
        }
        let alice = worker("alice", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let add = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "v1"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = add else {
            panic!("expected Ok, got {add:?}");
        };
        let note_id = data.unwrap()["note_id"].as_i64().unwrap();
        let edit = EditTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"note_id": note_id, "text": "v2"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(edit, ToolResult::Ok { .. }));
    }

    #[tokio::test]
    async fn a_non_author_edit_and_a_missing_comment_edit_are_indistinguishable() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", Some("alice"), "alice");
        }
        let alice = worker("alice", &[]);
        let bob = worker("bob", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let add = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "v1"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = add else {
            panic!("expected Ok, got {add:?}");
        };
        let note_id = data.unwrap()["note_id"].as_i64().unwrap();

        let foreign_edit = EditTaskCommentTool::call(
            Some(&bob),
            &serde_json::json!({"note_id": note_id, "text": "steal"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let missing_edit = EditTaskCommentTool::call(
            Some(&bob),
            &serde_json::json!({"note_id": note_id + 999, "text": "x"}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        let ToolResult::NotFound { hint: h1, .. } = foreign_edit else {
            panic!("expected NotFound");
        };
        let ToolResult::NotFound { hint: h2, .. } = missing_edit else {
            panic!("expected NotFound");
        };
        assert_eq!(h1, h2);
    }

    #[tokio::test]
    async fn an_admin_can_delete_someone_elses_comment() {
        let conn = setup().await;
        {
            let c = conn.lock().await;
            seed_task(&c, "t1", Some("alice"), "alice");
        }
        let alice = worker("alice", &[]);
        let op = operator();
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let add = AddTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"task_id": "t1", "text": "v1"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        let ToolResult::Ok { data, .. } = add else {
            panic!("expected Ok, got {add:?}");
        };
        let note_id = data.unwrap()["note_id"].as_i64().unwrap();
        let delete = DeleteTaskCommentTool::call(
            Some(&op),
            &serde_json::json!({"note_id": note_id}),
            &conn,
            "2026-06-01T00:01:00Z",
            &c,
        )
        .await;
        assert!(matches!(delete, ToolResult::Ok { .. }));
    }

    #[tokio::test]
    async fn a_non_integer_note_id_is_invalid_not_a_crash() {
        let conn = setup().await;
        let alice = worker("alice", &[]);
        let registry = WaiterRegistry::new();
        let file_map = FileMap::new();
        let c = ctx(&registry, &file_map);
        let result = EditTaskCommentTool::call(
            Some(&alice),
            &serde_json::json!({"note_id": "not-a-number", "text": "x"}),
            &conn,
            "2026-06-01T00:00:00Z",
            &c,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[test]
    fn schema_max_lengths_match_the_shared_constant() {
        let parsed: Value = serde_json::from_str(AddTaskCommentTool::SCHEMA).unwrap();
        let max_len = parsed["properties"]["task_id"]["maxLength"]
            .as_u64()
            .unwrap();
        assert_eq!(
            max_len as usize,
            conexus_core::schema_limits::IDENTIFIER_MAX_LEN
        );
    }
}
