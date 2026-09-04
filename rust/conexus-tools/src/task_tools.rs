//! Port of `agent_mcp/tools/task_tools.py` (Phase D4, 6,812 LOC in
//! Python -- the largest file in the codebase, deliberately ported
//! last among tool modules). This PR (1/8, per the Phase D4 research
//! pass) covers only the PURE, DB-free-or-single-query helpers every
//! later mutating/read tool composes: status-transition legality,
//! agent-assignability, dependency-cycle detection, the single-root
//! conflict guard, descendant collection for cascade delete, and the
//! small half of `features/task_queries.py`'s dependency
//! (`TERMINAL_TASK_STATUSES`/`status_filter_matches`/
//! `INCOMPLETE_STATUS_ALIASES`) -- NOT the full `TaskQueryEngine`
//! (a genuinely new-design slice of its own, PR 2).
//!
//! See the migration plan's Phase D4 section
//! (`/home/dennis/.claude/plans/prancy-napping-pie.md`) for the full
//! 8-PR breakdown and the 5 design decisions resolved before this PR
//! (no new `UnitOfWork` primitive yet; the established DB-audit-only
//! precedent still applies; `send_message` lands just before PR 8;
//! RAG-placement validation is deferred past the mechanical PR 7;
//! `view_tasks`'s pagination bug is ported as-is).

use conexus_core::tool_result::ToolResult;
use conexus_wakeloop::event_feed::UNASSIGNED_TASK_TERMINAL_STATUSES as TERMINAL_TASK_STATUSES;
use rusqlite::Connection;
use std::collections::HashSet;

use std::sync::LazyLock;

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::task_ownership::can_access_task;
use conexus_db::task_repository::{self, TaskNote, TaskRow};
use conexus_db::{agent_action_repository, project_settings_repository, StableOrderCache};
use serde_json::Value;
use tokio::sync::Mutex as AsyncMutex;

use crate::task_query_engine::{
    health_of, SortBy, TaskFilterSpec, TaskHealth, TaskQueryEngine, TaskSortSpec,
};

/// A `want` status filter that matches any non-terminal ("still being
/// worked") status, rather than one concrete value. Port of
/// `features/task_queries.py::INCOMPLETE_STATUS_ALIASES`.
pub const INCOMPLETE_STATUS_ALIASES: [&str; 3] = ["incomplete", "active", "open"];

/// The statuses [`INCOMPLETE_STATUS_ALIASES`] actually matches. Port
/// of `features/task_queries.py::_ACTIVE_STATUSES` -- deliberately a
/// SMALLER set than "not terminal" (e.g. excludes any future
/// in-between status), matching Python's own explicit enumeration
/// rather than a derived complement.
const ACTIVE_STATUSES: [&str; 2] = ["in_progress", "pending"];

/// Whether a task whose status is `actual` satisfies a `want` status
/// filter. `want` is either a concrete status (exact match) or one of
/// [`INCOMPLETE_STATUS_ALIASES`] (matches any of [`ACTIVE_STATUSES`]).
/// Shared by `view_tasks` (via the query engine, PR 2) and
/// `search_tasks` so the two surfaces interpret the pseudo-values
/// identically -- port of `status_filter_matches`.
pub fn status_filter_matches(want: &str, actual: Option<&str>) -> bool {
    if INCOMPLETE_STATUS_ALIASES.contains(&want) {
        return actual.is_some_and(|a| ACTIVE_STATUSES.contains(&a));
    }
    actual == Some(want)
}

/// Whether a task may move from `old_status` to `new_status`. Port of
/// `_is_status_transition_allowed`:
/// - A terminal source state (completed/cancelled/failed) is a sink --
///   every outgoing transition is rejected, INCLUDING a no-op write of
///   the same terminal state (a re-complete would re-fire the
///   dependency-advance side effects).
/// - A same-state write on a non-terminal state is an idempotent no-op
///   and is allowed (e.g. re-affirming `in_progress` while appending a
///   note).
/// - Any transition out of a non-terminal state is allowed.
pub fn is_status_transition_allowed(old_status: Option<&str>, new_status: &str) -> bool {
    if old_status == Some(new_status) {
        return !old_status.is_some_and(|s| TERMINAL_TASK_STATUSES.contains(&s));
    }
    if old_status.is_some_and(|s| TERMINAL_TASK_STATUSES.contains(&s)) {
        return false;
    }
    true
}

/// True iff `agent_id` exists and is a live (active) agent. Assignment
/// targets must be live agents -- a task pinned on a terminated agent
/// is unreachable work (and, for the audit trail, attributes to a
/// revoked identity); "live" also excludes tombstone rows
/// (`[deleted-<id>]` purge-cascade FK artifacts, BL-R31-3b). Port of
/// `_agent_assignable`, delegating to the same converged predicate
/// Python's own version delegates to (`agent_repository.is_live_agent`
/// -> here, `AgentRepository::is_live`).
pub fn agent_assignable(conn: &Connection, agent_id: &str) -> bool {
    conexus_db::agent_repository::AgentRepository::is_live(conn, agent_id).unwrap_or(false)
}

/// Return a [`ToolResult::Conflict`] if a root task already exists,
/// else `None`. Port of `_single_root_conflict`.
///
/// The single-root-task invariant (at most one `parent_task IS NULL`
/// per project DB) is enforced on every create path. This is the
/// shared check every parentless-create call site must run INSIDE its
/// own transaction so it sees uncommitted siblings and stays atomic
/// with the INSERT -- R15 Sibling 1b (TOCTOU): a caller that checks
/// this BEFORE any suspending work (e.g. a RAG placement-validation
/// call, PR 7) must re-run it immediately before the actual INSERT.
pub fn single_root_conflict(conn: &Connection) -> Option<ToolResult> {
    let (count, root_id): (i64, Option<String>) = conn
        .query_row(
            "SELECT COUNT(*) as count, MIN(task_id) as root_id FROM tasks WHERE parent_task IS NULL",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap_or((0, None));
    if count > 0 {
        let existing_root_id = root_id.unwrap_or_default();
        return Some(ToolResult::Conflict {
            reason: format!(
                "Cannot create root task. A root task already exists ({existing_root_id}). \
                 All new tasks must have a parent."
            ),
        });
    }
    None
}

/// Return `(task_id, assigned_to)` for every descendant of
/// `root_task_id`, ordered so front-to-back deletion never violates
/// the `tasks.parent_task` self-FK (deepest descendants first). Port
/// of `_collect_task_descendants`.
///
/// Source of truth is the `parent_task` FK column -- NOT the
/// `child_tasks` JSON mirror -- so a force-delete cascades correctly
/// even when the mirror has drifted (BL-2). A `seen` set guards
/// against a malformed parent cycle.
pub fn collect_task_descendants(
    conn: &Connection,
    root_task_id: &str,
) -> rusqlite::Result<Vec<(String, Option<String>)>> {
    let mut ordered: Vec<(String, Option<String>)> = Vec::new(); // BFS order: parent before child
    let mut seen: HashSet<String> = HashSet::from([root_task_id.to_string()]);
    let mut frontier = vec![root_task_id.to_string()];
    let mut stmt = conn.prepare("SELECT task_id, assigned_to FROM tasks WHERE parent_task = ?1")?;
    while !frontier.is_empty() {
        let mut next_frontier = Vec::new();
        for tid in &frontier {
            let rows = stmt.query_map([tid], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
            })?;
            for row in rows {
                let (child_id, assigned_to) = row?;
                if seen.contains(&child_id) {
                    continue;
                }
                seen.insert(child_id.clone());
                ordered.push((child_id.clone(), assigned_to));
                next_frontier.push(child_id);
            }
        }
        frontier = next_frontier;
    }
    ordered.reverse(); // deepest first -> safe delete order under the FK
    Ok(ordered)
}

/// Return the cycle chain if wiring `task_id`'s `depends_on_tasks` to
/// `proposed_deps` would introduce a cycle in the dependency graph,
/// else `None`. Port of `_find_dependency_cycle`.
///
/// R21-F2: `depends_on_tasks` previously accepted ANY list with no
/// graph validation at all -- two tasks could be pointed at each other
/// (or a task at itself) and silently persisted, producing meaningless
/// auto-advance ordering forever (never crashing or infinite-looping,
/// since nothing walks the graph transitively).
///
/// BFS over the EXISTING `depends_on_tasks` edges (read fresh from
/// `conn` inside the caller's own transaction), starting from each
/// proposed dependency. If the walk ever reaches back to `task_id`
/// itself -- the direct self-dependency case is caught on the very
/// first hop -- that is a cycle; the returned list is the chain
/// `[task_id, ..., task_id]` for a readable error message.
///
/// Shared by the create paths (called right after a fresh id is
/// minted but before the INSERT -- structurally guaranteed to find
/// nothing there today, applied uniformly anyway since a future
/// client-specified-id change could make that assumption stale) and
/// the update path (where an EXISTING task's edges can be re-pointed
/// to complete a cycle already latent in the graph).
pub fn find_dependency_cycle(
    conn: &Connection,
    task_id: &str,
    proposed_deps: &[String],
) -> rusqlite::Result<Option<Vec<String>>> {
    if proposed_deps.is_empty() {
        return Ok(None);
    }
    let mut visited: HashSet<String> = HashSet::new();
    let mut queue: Vec<Vec<String>> = proposed_deps
        .iter()
        .map(|dep| vec![task_id.to_string(), dep.clone()])
        .collect();
    let mut idx = 0;
    while idx < queue.len() {
        let path = queue[idx].clone();
        idx += 1;
        let node = path.last().expect("path is never empty");
        if node == task_id {
            return Ok(Some(path));
        }
        if visited.contains(node) {
            continue;
        }
        visited.insert(node.clone());
        // Two distinct "nothing to add" cases collapse to the same
        // outcome here, matching Python's `if not row: continue` +
        // `row["depends_on_tasks"] or "[]"`: the row may not exist at
        // all (deleted mid-BFS), or it may exist with a NULL
        // `depends_on_tasks` column -- either way there are no further
        // edges from this node.
        let row_result: rusqlite::Result<Option<String>> = conn.query_row(
            "SELECT depends_on_tasks FROM tasks WHERE task_id = ?1",
            [node.as_str()],
            |row| row.get(0),
        );
        let deps_json = match row_result {
            Ok(v) => v,
            Err(rusqlite::Error::QueryReturnedNoRows) => None,
            Err(e) => return Err(e),
        };
        let Some(deps_json) = deps_json else {
            continue;
        };
        let deps: Vec<String> = serde_json::from_str(&deps_json).unwrap_or_default();
        for next in deps {
            let mut extended = path.clone();
            extended.push(next);
            queue.push(extended);
        }
    }
    Ok(None)
}

// ======================================================================
// view_tasks / search_tasks (Phase D4, PR 3/8)
// ======================================================================
//
// Wires the pure helpers above and `TaskQueryEngine` (PR 2/8) into the
// two read-only `Tool` impls. Faithful port of
// `task_tools.py::view_tasks_tool_impl`/`search_tasks_tool_impl`,
// including the documented `start_after` residual bug (an outright-
// deleted anchor task silently restarts pagination from the top --
// see decision 5 in the migration plan's Phase D4 section: ported
// as-is, not fixed here).
//
// One deliberate re-derivation: Python's `estimate_tokens` calls
// `tiktoken.encoding_for_model("gpt-4")`, falling back to
// `len(text) // 4` only when tiktoken isn't importable. This port
// always takes that fallback path -- pulling a full BPE tokenizer into
// this workspace for a soft truncation heuristic (never a wire
// contract; it only decides how many tasks fit under `max_tokens`
// before a "response truncated" notice) isn't worth the dependency
// weight.

fn estimate_tokens(text: &str) -> usize {
    text.len() / 4
}

fn truncate_chars(s: &str, max: usize) -> String {
    if s.chars().count() > max {
        format!("{}...", s.chars().take(max).collect::<String>())
    } else {
        s.to_string()
    }
}

fn format_task_summary(task: &TaskRow) -> String {
    let description = truncate_chars(task.description.as_deref().unwrap_or("No description"), 100);
    format!(
        "ID: {}\nTitle: {}\nStatus: {} | Priority: {}\nAssigned to: {}\nDescription: {}",
        task.task_id,
        task.title,
        task.status,
        task.priority,
        task.assigned_to.as_deref().unwrap_or("Unassigned"),
        description,
    )
}

fn format_task_detailed(task: &TaskRow) -> String {
    let mut parts = vec![
        format!("ID: {}", task.task_id),
        format!("Title: {}", task.title),
        format!(
            "Description: {}",
            task.description.as_deref().unwrap_or("No description")
        ),
        format!("Status: {}", task.status),
        format!("Priority: {}", task.priority),
        format!(
            "Assigned to: {}",
            task.assigned_to.as_deref().unwrap_or("None")
        ),
        format!("Created by: {}", task.created_by),
        format!("Created: {}", task.created_at),
        format!("Updated: {}", task.updated_at),
    ];
    if let Some(parent) = &task.parent_task {
        parts.push(format!("Parent task: {parent}"));
    }
    if let Some(children) = task.child_tasks.as_ref().filter(|c| !c.is_empty()) {
        parts.push(format!("Child tasks: {}", children.join(", ")));
    }
    if let Some(notes) = task.notes.as_ref().filter(|n| !n.is_empty()) {
        parts.push("Notes:".to_string());
        let recent: &[TaskNote] = if notes.len() > 5 {
            &notes[notes.len() - 5..]
        } else {
            notes
        };
        for note in recent {
            parts.push(format!(
                "  - [{}] {}: {}",
                note.timestamp,
                note.author.as_deref().unwrap_or("Unknown"),
                note.content,
            ));
        }
        if notes.len() > 5 {
            parts.push(format!("  ... and {} more notes", notes.len() - 5));
        }
    }
    parts.join("\n")
}

fn fmt_capped_list(icon: &str, label: &str, items: &[String]) -> Option<String> {
    if items.is_empty() {
        return None;
    }
    let shown = items.iter().take(3).cloned().collect::<Vec<_>>().join(", ");
    let extra = if items.len() > 3 {
        format!(" (+{} more)", items.len() - 3)
    } else {
        String::new()
    };
    Some(format!("   {icon} {label}: {shown}{extra}"))
}

fn format_task_with_dependencies(task: &TaskRow, health: &TaskHealth) -> String {
    let mut text = format_task_detailed(task);
    let mut dep_parts = vec!["\n🔗 Dependency Analysis:".to_string()];
    let health_icon = match health.dependency_health.as_str() {
        "healthy" => "🟢",
        "waiting" => "🟡",
        "warning" => "🟠",
        _ => "🔴",
    };
    dep_parts.push(format!(
        "   Status: {health_icon} {}",
        health.dependency_health
    ));
    if health.is_blocked {
        dep_parts.push("   ⚠️  BLOCKED - Cannot proceed".to_string());
    } else if !health.can_start {
        dep_parts.push("   ⏳ WAITING - Dependencies not ready".to_string());
    } else {
        dep_parts.push("   ✅ READY - Can proceed".to_string());
    }
    for s in [
        fmt_capped_list("✅", "Completed", &health.completed_dependencies),
        fmt_capped_list("🔴", "Blocking", &health.blocking_dependencies),
        fmt_capped_list("❌", "Missing", &health.missing_dependencies),
        fmt_capped_list("🔒", "Blocks", &health.blocks_tasks),
    ]
    .into_iter()
    .flatten()
    {
        dep_parts.push(s);
    }
    text.push_str(&dep_parts.join("\n"));
    text
}

fn parse_sort_by(s: &str) -> SortBy {
    match s {
        "updated_at" => SortBy::UpdatedAt,
        "priority" => SortBy::Priority,
        "status" => SortBy::Status,
        _ => SortBy::CreatedAt,
    }
}

fn str_arg(arguments: &Value, key: &str) -> Option<String> {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn bool_arg(arguments: &Value, key: &str) -> bool {
    arguments.get(key).and_then(Value::as_bool).unwrap_or(false)
}

/// str.title()'s underscore-preserving capitalization for the 4
/// literal `health_status` values `health_metrics` produces --
/// Python's `"needs_attention".title()` yields `"Needs_Attention"`
/// (the underscore is a word boundary but stays literal), not
/// `"Needs Attention"`. A closed match over the 4 known values avoids
/// re-deriving that quirk generically.
fn title_case_health_status(status: &str) -> &'static str {
    match status {
        "excellent" => "Excellent",
        "good" => "Good",
        "needs_attention" => "Needs_Attention",
        "critical" => "Critical",
        _ => "Unknown",
    }
}

/// One process-wide instance, matching Python's module-level
/// `TaskQueryEngine(...)` -- the pagination cache must survive across
/// calls (an `offset > 0` call replays the ordering an earlier
/// `offset == 0` call anchored).
static VIEW_TASKS_ENGINE: LazyLock<TaskQueryEngine> = LazyLock::new(TaskQueryEngine::new);

/// Key for [`VIEW_TASKS_PAGE_CACHE`] -- factored into a named alias
/// purely to satisfy `clippy::type_complexity`; no behavior here.
type ViewTasksPageKey = (TaskFilterSpec, TaskSortSpec, Option<String>);

/// The TOOL's own outer pagination cache for `start_after`+`offset`/
/// `limit` windowing, keyed on `(filters, sort, start_after)` --
/// deliberately SEPARATE from `VIEW_TASKS_ENGINE`'s internal cache
/// (keyed on `(filters, sort)` alone), matching Python's own two
/// distinct caches (`TaskQueryEngine._pagination_cache` vs. the tool
/// module's own `_VIEW_TASKS_PAGINATION_CACHE`).
static VIEW_TASKS_PAGE_CACHE: LazyLock<StableOrderCache<ViewTasksPageKey, String>> =
    LazyLock::new(StableOrderCache::default);

pub struct ViewTasksTool;

impl Tool for ViewTasksTool {
    const NAME: &'static str = "view_tasks";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksView,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Smart task viewer with dependency analysis, health \
        metrics, and advanced filtering. For an overview against a project with many tasks, \
        prefer summary=true (and limit=50) to keep the response well under the per-call token \
        cap.\nCommon filters (combine freely):\n\
        - assigned=true: just YOUR OWN tasks.\n\
        - assigned=true + status=incomplete: your OPEN tasks.\n\
        - unassigned=true: the claimable pool.\n\
        - status=incomplete: all non-terminal tasks (alias active/open).\n\
        - created_by=<agent_id>: tasks a given agent filed.\n\
        - agent_id=<other agent>: another agent's tasks (default-on; policy may disable).";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Filter tasks by agent ID (optional)."},
            "status": {"type": "string", "description": "Filter by status; accepts a concrete status or incomplete/active/open for all non-terminal work.", "enum": ["pending","in_progress","completed","cancelled","failed","incomplete","active","open"]},
            "created_by": {"type": "string", "description": "Filter to tasks created by this agent ID."},
            "unassigned": {"type": "boolean", "description": "If true, return only unassigned (claimable-pool) tasks.", "default": false},
            "assigned": {"type": "boolean", "description": "If true, return only tasks that HAVE an assignee.", "default": false},
            "max_tokens": {"type": "integer", "description": "Maximum response tokens (default: 25000)", "minimum": 1000, "maximum": 25000},
            "start_after": {"type": "string", "description": "Task ID to start after (for pagination)."},
            "summary_mode": {"type": "boolean", "description": "If true, show only summary info (default: false)."},
            "summary": {"type": "boolean", "description": "Alias of summary_mode.", "default": false},
            "limit": {"type": "integer", "description": "Max tasks to return after filters + sort.", "minimum": 1},
            "offset": {"type": "integer", "description": "Pagination offset, applied after filters + sort.", "minimum": 0, "default": 0},
            "filter_priority": {"type": "string", "description": "Filter by priority level.", "enum": ["low","medium","high"]},
            "filter_parent_task": {"type": "string", "description": "Filter by parent task ID."},
            "show_blocked_tasks": {"type": "boolean", "description": "Show only blocked/waiting tasks."},
            "show_dependencies": {"type": "boolean", "description": "Include dependency chain analysis."},
            "show_health_analysis": {"type": "boolean", "description": "Include overall task health metrics."},
            "sort_by": {"type": "string", "description": "Sort tasks by field.", "enum": ["created_at","updated_at","priority","status"], "default": "created_at"}
        },
        "required": [],
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
            let principal = principal.expect("Cap-gated tool always has a resolved principal");
            let filter_agent_id = str_arg(arguments, "agent_id");
            let filter_status = str_arg(arguments, "status");
            let filter_created_by = str_arg(arguments, "created_by");
            let filter_unassigned = bool_arg(arguments, "unassigned");
            let filter_assigned = bool_arg(arguments, "assigned");
            let max_tokens = arguments
                .get("max_tokens")
                .and_then(Value::as_u64)
                .unwrap_or(25000) as usize;
            let start_after = str_arg(arguments, "start_after");
            let summary_mode =
                bool_arg(arguments, "summary_mode") || bool_arg(arguments, "summary");
            let limit = arguments.get("limit").and_then(Value::as_i64);
            let offset = arguments.get("offset").and_then(Value::as_i64).unwrap_or(0);
            let show_dependencies = bool_arg(arguments, "show_dependencies");
            let show_health_analysis = bool_arg(arguments, "show_health_analysis");
            let filter_priority = str_arg(arguments, "filter_priority");
            let filter_parent_task = str_arg(arguments, "filter_parent_task");
            let show_blocked_tasks = bool_arg(arguments, "show_blocked_tasks");
            let sort_by = str_arg(arguments, "sort_by").unwrap_or_else(|| "created_at".to_string());

            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let conn = conn.lock().await;

            let mut target_agent_id_for_filter = filter_agent_id.clone();
            if !is_admin_request
                && !project_settings_repository::get_bool(
                    &conn,
                    "config_allow_worker_view_foreign_tasks",
                    true,
                )
            {
                match &filter_agent_id {
                    None => target_agent_id_for_filter = Some(requesting_agent_id.clone()),
                    Some(fid) if fid != &requesting_agent_id => {
                        return ToolResult::PermissionDenied {
                            reason: "Non-admin agents can only view their own tasks. Omit the \
                                agent_id filter to see your own tasks plus the unassigned \
                                (claimable) pool. Ask admin to enable \
                                config_allow_worker_view_foreign_tasks to see other agents' \
                                tasks too."
                                .to_string(),
                        };
                    }
                    _ => {}
                }
            }

            let filters_spec = TaskFilterSpec {
                status: filter_status.clone(),
                priority: filter_priority.clone(),
                agent_id: target_agent_id_for_filter,
                parent_task_id: filter_parent_task.clone(),
                blocked_only: show_blocked_tasks,
                include_unassigned: !is_admin_request,
                created_by: filter_created_by,
                unassigned: filter_unassigned,
                assigned: filter_assigned,
            };
            let sort_spec = TaskSortSpec {
                by: parse_sort_by(&sort_by),
            };

            let query_result =
                match VIEW_TASKS_ENGINE.query(&conn, &filters_spec, &sort_spec, 0, None) {
                    Ok(r) => r,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error reading tasks".to_string(),
                        }
                    }
                };
            let mut tasks_to_display = query_result.tasks;

            // Dependency analysis needs the FULL snapshot (not just the
            // filtered window) to compute `blocks_tasks` correctly
            // across the whole graph.
            let dependency_analyses: Option<std::collections::HashMap<String, TaskHealth>> =
                if show_dependencies {
                    let full_snapshot: std::collections::HashMap<String, TaskRow> =
                        match task_repository::list_all(&conn, None) {
                            Ok(rows) => rows.into_iter().map(|t| (t.task_id.clone(), t)).collect(),
                            Err(_) => {
                                return ToolResult::Failed {
                                    message: "Database error reading tasks".to_string(),
                                }
                            }
                        };
                    Some(
                        tasks_to_display
                            .iter()
                            .map(|t| (t.task_id.clone(), health_of(t, &full_snapshot)))
                            .collect(),
                    )
                } else {
                    None
                };

            // Legacy token-style pagination: `start_after=<id>` skips
            // to the first task after the named one. Documented
            // residual bug ported as-is (decision 5): if the anchor
            // task was deleted OUTRIGHT (not merely re-filtered)
            // between calls, the loop below never finds it and
            // pagination silently restarts from the top.
            if let Some(anchor) = &start_after {
                let start_index = tasks_to_display
                    .iter()
                    .position(|t| &t.task_id == anchor)
                    .map(|i| i + 1)
                    .unwrap_or(0);
                tasks_to_display =
                    tasks_to_display.split_off(start_index.min(tasks_to_display.len()));
            }

            // Page-based pagination (offset/limit), anchored per
            // (filters, sort, start_after) via the tool's own
            // StableOrderCache -- R17-F2.
            let tasks_by_id: std::collections::HashMap<String, TaskRow> = tasks_to_display
                .iter()
                .cloned()
                .map(|t| (t.task_id.clone(), t))
                .collect();
            let page_key = (filters_spec, sort_spec, start_after.clone());
            let ordered_ids: Vec<String> =
                match VIEW_TASKS_PAGE_CACHE.get_or_anchor(page_key, offset, || {
                    Ok::<_, rusqlite::Error>(
                        tasks_to_display.iter().map(|t| t.task_id.clone()).collect(),
                    )
                }) {
                    Ok(ids) => ids,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error reading tasks".to_string(),
                        }
                    }
                };
            let total_matching = ordered_ids
                .iter()
                .filter(|id| tasks_by_id.contains_key(*id))
                .count();
            let offset_usize = offset.max(0) as usize;
            let mut window_ids: Vec<&String> = if offset > 0 {
                ordered_ids.iter().skip(offset_usize).collect()
            } else {
                ordered_ids.iter().collect()
            };
            if let Some(limit) = limit {
                window_ids.truncate(limit.max(0) as usize);
            }
            let tasks_to_display: Vec<TaskRow> = window_ids
                .into_iter()
                .filter_map(|id| tasks_by_id.get(id).cloned())
                .collect();

            let response_text = if tasks_to_display.is_empty() {
                "No tasks found matching the criteria.".to_string()
            } else {
                let health_analysis = if show_health_analysis {
                    Some(TaskQueryEngine::health_metrics(&tasks_to_display, now))
                } else {
                    None
                };

                let mut filter_info = Vec::new();
                if let Some(s) = &filter_status {
                    filter_info.push(format!("status={s}"));
                }
                if let Some(p) = &filter_priority {
                    filter_info.push(format!("priority={p}"));
                }
                if let Some(a) = &filter_agent_id {
                    filter_info.push(format!("agent={a}"));
                }
                if let Some(p) = &filter_parent_task {
                    filter_info.push(format!("parent={p}"));
                }
                if show_blocked_tasks {
                    filter_info.push("blocked_only=true".to_string());
                }

                let mut header = format!("Tasks ({} found", tasks_to_display.len());
                if !filter_info.is_empty() {
                    header.push_str(&format!(", filtered by: {}", filter_info.join(", ")));
                }
                header.push_str(&format!(", sorted by: {sort_by})"));

                let mut response_parts = vec![format!("{header}\n")];

                if let Some(health) = &health_analysis {
                    let score = health["health_score"].as_f64().unwrap_or(0.0);
                    let status = health["health_status"].as_str().unwrap_or("unknown");
                    let icon = match status {
                        "excellent" => "🟢",
                        "good" => "🟡",
                        "needs_attention" => "🟠",
                        _ => "🔴",
                    };
                    response_parts.push(format!(
                        "📊 **Health Analysis:** {icon} {} ({score}/100)",
                        title_case_health_status(status)
                    ));
                    // NOTE: `status_distribution` renders as compact
                    // JSON (`{"completed":1,...}`), not Python's dict
                    // repr (`{'completed': 1, ...}`) -- a presentation-
                    // only difference (this is response TEXT, not a
                    // wire contract) deliberately not chased further.
                    response_parts.push(format!("   Status: {}", health["status_distribution"]));
                    response_parts.push(format!(
                        "   Issues: {} blocked, {} stale",
                        health["blocked_tasks"], health["stale_tasks"]
                    ));
                    response_parts.push(String::new());
                }

                let mut current_tokens = estimate_tokens(&response_parts.join("\n"));
                let mut tasks_included = 0usize;
                let mut last_task_id: Option<String> = None;
                let mut truncated = false;

                for task in &tasks_to_display {
                    let task_text = if show_dependencies {
                        let health = dependency_analyses
                            .as_ref()
                            .and_then(|m| m.get(&task.task_id))
                            .cloned()
                            .unwrap_or_default();
                        format_task_with_dependencies(task, &health)
                    } else if summary_mode {
                        format_task_summary(task)
                    } else {
                        format_task_detailed(task)
                    };
                    let task_tokens = estimate_tokens(&task_text);
                    let safety_buffer = 1000;
                    if current_tokens + task_tokens > max_tokens.saturating_sub(safety_buffer)
                        && tasks_included > 0
                    {
                        truncated = true;
                        break;
                    }
                    response_parts.push(format!("{task_text}\n"));
                    current_tokens += task_tokens;
                    tasks_included += 1;
                    last_task_id = Some(task.task_id.clone());
                }

                if truncated {
                    let remaining_count = tasks_to_display.len() - tasks_included;
                    response_parts.push(format!(
                        "--- Response truncated to stay under {max_tokens} tokens ---"
                    ));
                    response_parts.push(format!(
                        "Showing {tasks_included} of {} tasks ({remaining_count} remaining)",
                        tasks_to_display.len()
                    ));
                    response_parts.push(format!(
                        "Continue: view_tasks(start_after='{}', max_tokens={max_tokens})",
                        last_task_id.unwrap_or_default()
                    ));
                    if !summary_mode {
                        response_parts.push("Overview: view_tasks(summary_mode=true)".to_string());
                    }
                } else {
                    response_parts
                        .push(format!("--- All {tasks_included} matching tasks shown ---"));
                }

                if let Some(limit) = limit {
                    response_parts.push(format!(
                        "Total: {total_matching} (showing offset={offset}, limit={limit})"
                    ));
                }

                response_parts.push("\n💡 Smart Tips:".to_string());
                if !show_dependencies {
                    response_parts
                        .push("• Add show_dependencies=true to see dependency chains".to_string());
                }
                if !show_health_analysis {
                    response_parts
                        .push("• Add show_health_analysis=true for health metrics".to_string());
                }
                if !show_blocked_tasks {
                    response_parts.push(
                        "• Add show_blocked_tasks=true to see only blocked tasks".to_string(),
                    );
                }
                response_parts.push(
                    "• Use sort_by=[priority|status|updated_at] for different sorting".to_string(),
                );
                if !filter_assigned && !filter_unassigned {
                    response_parts.push(
                        "• assigned=true = just your own tasks; add status=incomplete for your \
                         open tasks; unassigned=true = the claimable pool you can self-assign"
                            .to_string(),
                    );
                }

                response_parts.join("\n")
            };

            if let Err(_e) = agent_action_repository::log_agent_action(
                &conn,
                &requesting_agent_id,
                "view_tasks",
                None,
                Some(&serde_json::json!({
                    "filter_agent_id": filter_agent_id,
                    "filter_status": filter_status,
                })),
                now,
            ) {
                // Best-effort audit log -- matches Python's own
                // fire-and-forget log_audit semantics (never fails the
                // primary read for an audit-log write error).
            }

            ToolResult::Ok {
                data: None,
                message: Some(response_text),
            }
        })
    }
}

pub struct SearchTasksTool;

impl Tool for SearchTasksTool {
    const NAME: &'static str = "search_tasks";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksView,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Full-text search across task titles, descriptions, and \
        notes -- or filter-only listing when no query is supplied. Filter-only recipes (no \
        search_query needed): unassigned=true (claimable pool), assigned=true (your own \
        tasks), assigned=true + status_filter=incomplete (your open tasks), \
        status_filter=incomplete (all open work), created_by=<agent_id>.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "search_query": {"type": "string", "description": "Search terms to find in tasks. Optional -- when omitted, filters other than a query are used."},
            "status_filter": {"type": "string", "description": "Optional status filter; accepts a concrete status or incomplete/active/open.", "enum": ["pending","in_progress","completed","cancelled","failed","incomplete","active","open"]},
            "created_by": {"type": "string", "description": "Filter to tasks created by this agent ID."},
            "unassigned": {"type": "boolean", "description": "If true, return only unassigned (claimable-pool) tasks.", "default": false},
            "assigned": {"type": "boolean", "description": "If true, return only tasks that HAVE an assignee.", "default": false},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 20)", "minimum": 1, "maximum": 100},
            "include_notes": {"type": "boolean", "description": "Include notes content in search (default: true)."}
        },
        "required": [],
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
            let principal = principal.expect("Cap-gated tool always has a resolved principal");
            let search_query = str_arg(arguments, "search_query");
            let status_filter = str_arg(arguments, "status_filter");
            let filter_created_by = str_arg(arguments, "created_by");
            let filter_unassigned = bool_arg(arguments, "unassigned");
            let filter_assigned = bool_arg(arguments, "assigned");
            let max_results = arguments
                .get("max_results")
                .and_then(Value::as_i64)
                .unwrap_or(20)
                .max(0) as usize;
            let include_notes = arguments
                .get("include_notes")
                .and_then(Value::as_bool)
                .unwrap_or(true);

            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let has_query = search_query
                .as_deref()
                .is_some_and(|q| !q.trim().is_empty());
            let search_terms: Vec<String> = if has_query {
                search_query
                    .as_deref()
                    .unwrap()
                    .split_whitespace()
                    .map(|t| t.trim().to_lowercase())
                    .filter(|t| t.len() > 2)
                    .collect()
            } else {
                Vec::new()
            };
            if has_query && search_terms.is_empty() {
                return ToolResult::Invalid {
                    field: Some("search_query".to_string()),
                    message: "Search query must contain terms longer than 2 characters."
                        .to_string(),
                };
            }

            if !has_query
                && status_filter.is_none()
                && filter_created_by.is_none()
                && !filter_unassigned
                && !filter_assigned
            {
                return ToolResult::Invalid {
                    field: None,
                    message: "search_tasks requires at least one of: search_query, \
                        status_filter, created_by, unassigned, or assigned. For an unfiltered \
                        listing of tasks, use view_tasks instead."
                        .to_string(),
                };
            }

            let conn = conn.lock().await;
            let allow_foreign = project_settings_repository::get_bool(
                &conn,
                "config_allow_worker_view_foreign_tasks",
                true,
            );
            let all_tasks = match task_repository::list_all(&conn, None) {
                Ok(rows) => rows,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error reading tasks".to_string(),
                    }
                }
            };

            let mut candidate_tasks: Vec<TaskRow> = Vec::new();
            for task in all_tasks {
                if !is_admin_request
                    && !can_access_task(
                        task.assigned_to.as_deref(),
                        Some(task.created_by.as_str()),
                        Some(requesting_agent_id.as_str()),
                        false,
                        false,
                        true,
                        allow_foreign,
                    )
                {
                    continue;
                }
                if let Some(sf) = &status_filter {
                    if !status_filter_matches(sf, Some(task.status.as_str())) {
                        continue;
                    }
                }
                if let Some(cb) = &filter_created_by {
                    if &task.created_by != cb {
                        continue;
                    }
                }
                if filter_unassigned && task.assigned_to.as_deref().is_some_and(|v| !v.is_empty()) {
                    continue;
                }
                if filter_assigned && task.assigned_to.as_deref().is_none_or(str::is_empty) {
                    continue;
                }
                candidate_tasks.push(task);
            }

            if candidate_tasks.is_empty() {
                return ToolResult::Ok {
                    data: None,
                    message: Some("No tasks found matching the criteria.".to_string()),
                };
            }

            let response_text = if !has_query {
                candidate_tasks.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
                let truncated: Vec<&TaskRow> = candidate_tasks.iter().take(max_results).collect();
                let filter_descr = status_filter
                    .as_ref()
                    .map(|s| format!("status={s}"))
                    .unwrap_or_else(|| "no filters".to_string());

                let mut response_parts = vec![format!(
                    "Tasks matching filters ({filter_descr}) — {} of {} shown:\n",
                    truncated.len(),
                    candidate_tasks.len()
                )];
                let mut current_tokens = estimate_tokens(&response_parts.join("\n"));
                for (i, task) in truncated.iter().enumerate() {
                    let mut task_text =
                        format!("\n{}. **{}** (ID: {})", i + 1, task.title, task.task_id);
                    task_text.push_str(&format!(
                        "\n   Status: {} | Priority: {} | Assigned: {}",
                        task.status,
                        task.priority,
                        task.assigned_to.as_deref().unwrap_or("None")
                    ));
                    let desc = truncate_chars(
                        task.description.as_deref().unwrap_or("No description"),
                        200,
                    );
                    task_text.push_str(&format!("\n   Description: {desc}"));

                    let task_tokens = estimate_tokens(&task_text);
                    let safety_buffer = 1000;
                    if current_tokens + task_tokens <= 20000usize.saturating_sub(safety_buffer) {
                        response_parts.push(task_text);
                        current_tokens += task_tokens;
                    } else {
                        let remaining = truncated.len() - i;
                        response_parts.push(format!(
                            "\n⚠️  Response truncated - {remaining} more results available"
                        ));
                        break;
                    }
                }
                response_parts.push("\n\n💡 Tips:".to_string());
                response_parts
                    .push("• Use view_tasks(task_id='ID') for full task details".to_string());
                response_parts
                    .push("• Add search_query to score results by text relevance".to_string());
                response_parts.push("• Use max_results to control response size".to_string());

                if let Err(_e) = agent_action_repository::log_agent_action(
                    &conn,
                    &requesting_agent_id,
                    "search_tasks",
                    None,
                    Some(&serde_json::json!({
                        "query": Value::Null,
                        "status_filter": status_filter,
                        "results": truncated.len(),
                    })),
                    now,
                ) {
                    // Best-effort audit log, same rationale as
                    // view_tasks above.
                }
                response_parts.join("\n")
            } else {
                let mut scored: Vec<(TaskRow, f64, Vec<String>)> = Vec::new();
                for task in candidate_tasks {
                    let mut score = 0.0f64;
                    let mut matched_fields = Vec::new();

                    let title = task.title.to_lowercase();
                    let title_matches = search_terms
                        .iter()
                        .filter(|t| title.contains(t.as_str()))
                        .count();
                    if title_matches > 0 {
                        score += title_matches as f64 * 3.0;
                        matched_fields.push(format!("title ({title_matches} terms)"));
                    }

                    let description = task.description.clone().unwrap_or_default().to_lowercase();
                    let desc_matches = search_terms
                        .iter()
                        .filter(|t| description.contains(t.as_str()))
                        .count();
                    if desc_matches > 0 {
                        score += desc_matches as f64 * 2.0;
                        matched_fields.push(format!("description ({desc_matches} terms)"));
                    }

                    if include_notes {
                        let notes_content = match &task.notes {
                            Some(notes) => notes
                                .iter()
                                .map(|n| n.content.as_str())
                                .collect::<Vec<_>>()
                                .join(" ")
                                .to_lowercase(),
                            None => String::new(),
                        };
                        let notes_matches = search_terms
                            .iter()
                            .filter(|t| notes_content.contains(t.as_str()))
                            .count();
                        if notes_matches > 0 {
                            score += notes_matches as f64;
                            matched_fields.push(format!("notes ({notes_matches} terms)"));
                        }
                    }

                    let full_text = format!("{title} {description}");
                    if full_text.contains(search_query.as_deref().unwrap().to_lowercase().as_str())
                    {
                        score += 2.0;
                        matched_fields.push("exact phrase".to_string());
                    }

                    if score > 0.0 {
                        scored.push((task, score, matched_fields));
                    }
                }

                if scored.is_empty() {
                    return ToolResult::Ok {
                        data: None,
                        message: Some(format!(
                            "No tasks found containing '{}'.",
                            search_query.as_deref().unwrap()
                        )),
                    };
                }

                // Sort by (score, updated_at) descending -- Python's
                // tuple-key `sort(reverse=True)` compares both fields
                // in the same direction. `score` is always a finite
                // f64 built from integer term counts, so the
                // `partial_cmp` fallback never actually fires.
                scored.sort_by(|a, b| {
                    b.1.partial_cmp(&a.1)
                        .unwrap_or(std::cmp::Ordering::Equal)
                        .then_with(|| b.0.updated_at.cmp(&a.0.updated_at))
                });
                scored.truncate(max_results);

                let mut response_parts = vec![format!(
                    "Search Results for '{}' ({} found):\n",
                    search_query.as_deref().unwrap(),
                    scored.len()
                )];
                let mut current_tokens = estimate_tokens(&response_parts.join("\n"));

                for (i, (task, score, matched_fields)) in scored.iter().enumerate() {
                    if current_tokens >= 20000 {
                        let remaining = scored.len() - i;
                        response_parts.push(format!(
                            "\n⚠️  Response truncated - {remaining} more results available"
                        ));
                        response_parts.push(
                            "Use max_results parameter or refine search to see more".to_string(),
                        );
                        break;
                    }

                    let mut task_text =
                        format!("\n{}. **{}** (ID: {})", i + 1, task.title, task.task_id);
                    task_text.push_str(&format!(
                        "\n   Status: {} | Priority: {} | Assigned: {}",
                        task.status,
                        task.priority,
                        task.assigned_to.as_deref().unwrap_or("None")
                    ));
                    task_text.push_str(&format!(
                        "\n   Relevance Score: {score:.1} | Matched: {}",
                        matched_fields.join(", ")
                    ));
                    let desc = truncate_chars(
                        task.description.as_deref().unwrap_or("No description"),
                        200,
                    );
                    task_text.push_str(&format!("\n   Description: {desc}"));

                    let task_tokens = estimate_tokens(&task_text);
                    let safety_buffer = 1000;
                    if current_tokens + task_tokens <= 20000usize.saturating_sub(safety_buffer) {
                        response_parts.push(task_text);
                        current_tokens += task_tokens;
                    } else {
                        let remaining = scored.len() - i;
                        response_parts.push(format!(
                            "\n⚠️  Response truncated - {remaining} more results available"
                        ));
                        break;
                    }
                }

                response_parts.push("\n\n💡 Tips:".to_string());
                response_parts
                    .push("• Use view_tasks(task_id='ID') for full task details".to_string());
                response_parts.push(
                    "• Filters (combine, no query needed): unassigned=true (claimable pool), \
                     assigned=true (your own tasks), assigned=true + status_filter=incomplete \
                     (your open tasks), created_by=<agent>"
                        .to_string(),
                );
                response_parts.push("• Use max_results to control response size".to_string());

                if let Err(_e) = agent_action_repository::log_agent_action(
                    &conn,
                    &requesting_agent_id,
                    "search_tasks",
                    None,
                    Some(&serde_json::json!({
                        "query": search_query,
                        "results": scored.len(),
                    })),
                    now,
                ) {
                    // Best-effort audit log, same rationale as above.
                }

                response_parts.join("\n")
            };

            ToolResult::Ok {
                data: None,
                message: Some(response_text),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{self, NewTask};

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    fn new_task<'a>(
        id: &'a str,
        title: &'a str,
        parent: Option<&'a str>,
        deps: Option<&'a [String]>,
    ) -> NewTask<'a> {
        NewTask {
            task_id: Some(id),
            title,
            description: None,
            assigned_to: None,
            created_by: "bob",
            status: "pending",
            priority: "medium",
            parent_task: parent,
            child_tasks: None,
            depends_on_tasks: deps,
            notes: None,
            now: "2026-01-01T00:00:00Z",
        }
    }

    // -- status_filter_matches --------------------------------------------

    #[test]
    fn status_filter_matches_exact_status() {
        assert!(status_filter_matches("completed", Some("completed")));
        assert!(!status_filter_matches("completed", Some("pending")));
    }

    #[test]
    fn status_filter_matches_incomplete_aliases_match_active_statuses() {
        for alias in INCOMPLETE_STATUS_ALIASES {
            assert!(status_filter_matches(alias, Some("pending")));
            assert!(status_filter_matches(alias, Some("in_progress")));
            assert!(!status_filter_matches(alias, Some("completed")));
        }
    }

    #[test]
    fn status_filter_matches_none_actual_never_matches() {
        assert!(!status_filter_matches("pending", None));
        assert!(!status_filter_matches("incomplete", None));
    }

    // -- is_status_transition_allowed -------------------------------------

    #[test]
    fn terminal_source_is_a_sink_for_every_outgoing_transition() {
        for terminal in ["completed", "cancelled", "failed"] {
            assert!(!is_status_transition_allowed(Some(terminal), "pending"));
            assert!(!is_status_transition_allowed(Some(terminal), "in_progress"));
        }
    }

    #[test]
    fn a_terminal_same_state_rewrite_is_rejected_not_a_noop() {
        // A re-complete must NOT be treated as an allowed idempotent
        // no-op -- it would re-fire dependency-advance side effects.
        assert!(!is_status_transition_allowed(
            Some("completed"),
            "completed"
        ));
    }

    #[test]
    fn a_non_terminal_same_state_rewrite_is_an_allowed_noop() {
        assert!(is_status_transition_allowed(
            Some("in_progress"),
            "in_progress"
        ));
    }

    #[test]
    fn any_transition_out_of_a_non_terminal_state_is_allowed() {
        assert!(is_status_transition_allowed(Some("pending"), "in_progress"));
        assert!(is_status_transition_allowed(Some("pending"), "completed"));
    }

    #[test]
    fn a_brand_new_task_with_no_old_status_is_allowed() {
        assert!(is_status_transition_allowed(None, "pending"));
    }

    // -- agent_assignable ---------------------------------------------------

    #[test]
    fn agent_assignable_true_for_a_live_agent() {
        let conn = test_conn();
        conexus_db::agent_repository::AgentRepository::create(
            &conn,
            conexus_db::agent_repository::NewAgent {
                token: "tok",
                agent_id: "alice",
                created_at: "2026-01-01T00:00:00Z",
                status: "active",
                current_task: None,
                working_directory: "/tmp",
                color: None,
                agent_role: "worker",
            },
        )
        .unwrap();
        assert!(agent_assignable(&conn, "alice"));
    }

    #[test]
    fn agent_assignable_false_for_an_unknown_agent() {
        let conn = test_conn();
        assert!(!agent_assignable(&conn, "nobody"));
    }

    // -- single_root_conflict -----------------------------------------------

    #[test]
    fn single_root_conflict_none_when_no_root_exists() {
        let conn = test_conn();
        assert!(single_root_conflict(&conn).is_none());
    }

    #[test]
    fn single_root_conflict_some_when_a_root_already_exists() {
        let conn = test_conn();
        task_repository::create(&conn, new_task("task_1", "root", None, None)).unwrap();
        let result = single_root_conflict(&conn);
        assert!(matches!(result, Some(ToolResult::Conflict { .. })));
    }

    // -- collect_task_descendants --------------------------------------------

    #[test]
    fn collect_task_descendants_orders_deepest_first() {
        let conn = test_conn();
        task_repository::create(&conn, new_task("root", "root", None, None)).unwrap();
        task_repository::create(&conn, new_task("child", "child", Some("root"), None)).unwrap();
        task_repository::create(&conn, new_task("grandchild", "gc", Some("child"), None)).unwrap();

        let descendants = collect_task_descendants(&conn, "root").unwrap();
        let ids: Vec<&str> = descendants.iter().map(|(id, _)| id.as_str()).collect();
        assert_eq!(ids, vec!["grandchild", "child"]);
    }

    #[test]
    fn collect_task_descendants_empty_for_a_leaf_task() {
        let conn = test_conn();
        task_repository::create(&conn, new_task("root", "root", None, None)).unwrap();
        assert!(collect_task_descendants(&conn, "root").unwrap().is_empty());
    }

    #[test]
    fn collect_task_descendants_uses_fk_not_the_child_tasks_json_mirror() {
        // BL-2: even if child_tasks were stale/absent, the FK-derived
        // walk still finds the real child.
        let conn = test_conn();
        task_repository::create(&conn, new_task("root", "root", None, None)).unwrap();
        task_repository::create(&conn, new_task("child", "child", Some("root"), None)).unwrap();
        // No child_tasks mirror was ever written on "root" -- the walk
        // must still find "child" via parent_task alone.
        let descendants = collect_task_descendants(&conn, "root").unwrap();
        assert_eq!(descendants.len(), 1);
        assert_eq!(descendants[0].0, "child");
    }

    // -- find_dependency_cycle ------------------------------------------------

    #[test]
    fn find_dependency_cycle_none_for_an_empty_proposal() {
        let conn = test_conn();
        assert!(find_dependency_cycle(&conn, "task_1", &[])
            .unwrap()
            .is_none());
    }

    #[test]
    fn find_dependency_cycle_detects_direct_self_dependency() {
        let conn = test_conn();
        let cycle = find_dependency_cycle(&conn, "task_1", &["task_1".to_string()])
            .unwrap()
            .unwrap();
        assert_eq!(cycle, vec!["task_1", "task_1"]);
    }

    #[test]
    fn find_dependency_cycle_detects_a_two_hop_cycle() {
        let conn = test_conn();
        // task_2 already depends on task_1 -- proposing task_1 depends
        // on task_2 would close the loop.
        task_repository::create(
            &conn,
            new_task("task_2", "t2", None, Some(&["task_1".to_string()])),
        )
        .unwrap();
        let cycle = find_dependency_cycle(&conn, "task_1", &["task_2".to_string()])
            .unwrap()
            .unwrap();
        assert_eq!(cycle, vec!["task_1", "task_2", "task_1"]);
    }

    #[test]
    fn find_dependency_cycle_none_for_a_genuinely_acyclic_graph() {
        let conn = test_conn();
        task_repository::create(&conn, new_task("task_2", "t2", None, None)).unwrap();
        assert!(
            find_dependency_cycle(&conn, "task_1", &["task_2".to_string()])
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn find_dependency_cycle_is_structurally_impossible_for_a_fresh_id_with_no_incoming_edges() {
        // Applied uniformly at creation time even though today a brand
        // new id can have no existing incoming edges yet.
        let conn = test_conn();
        task_repository::create(&conn, new_task("task_2", "t2", None, None)).unwrap();
        task_repository::create(&conn, new_task("task_3", "t3", Some("task_2"), None)).unwrap();
        assert!(find_dependency_cycle(
            &conn,
            "brand_new_task",
            &["task_2".to_string(), "task_3".to_string()],
        )
        .unwrap()
        .is_none());
    }
}

#[cfg(test)]
mod view_search_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{NewTask, TaskFields};
    use conexus_wakeloop::waiter_registry::WaiterRegistry;

    const NOW: &str = "2026-01-15T00:00:00Z";

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    fn worker(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::from_iter([Capability::TasksView]),
        }
    }

    fn admin(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::from_iter([Capability::TasksView, Capability::TasksAssign]),
        }
    }

    // The `idx_tasks_single_root` invariant (R15-BL-1) allows at most
    // one task with a NULL `parent_task` -- every test-seeded task
    // hangs off one shared, idempotently-created root instead of each
    // being its own root (which would collide on the second insert).
    fn ensure_root(conn: &Connection) {
        if task_repository::get_by_id(conn, "root_anchor")
            .unwrap()
            .is_some()
        {
            return;
        }
        task_repository::create(
            conn,
            NewTask {
                task_id: Some("root_anchor"),
                title: "root anchor",
                description: None,
                assigned_to: None,
                created_by: "system",
                status: "pending",
                priority: "medium",
                parent_task: None,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    fn seed(
        conn: &Connection,
        id: &str,
        assigned_to: Option<&str>,
        created_by: &str,
        status: &str,
    ) {
        ensure_root(conn);
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
                parent_task: Some("root_anchor"),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    fn disallow_foreign_view(conn: &Connection) {
        project_settings_repository::upsert(
            conn,
            "config_allow_worker_view_foreign_tasks",
            "false",
            None,
            false,
            "test",
            NOW,
        )
        .unwrap();
    }

    fn message_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    // -- view_tasks -----------------------------------------------------

    #[tokio::test]
    async fn view_tasks_empty_store_reports_no_tasks() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(Some(&admin("a")), &Value::Null, &conn, NOW, &ctx).await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: None,
                message: Some("No tasks found matching the criteria.".to_string()),
            }
        );
    }

    #[tokio::test]
    async fn view_tasks_admin_sees_tasks_across_every_agent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", Some("alice"), "bob", "pending");
            seed(&guard, "t2", Some("carol"), "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"created_by": "bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Task t1"));
        assert!(msg.contains("Task t2"));
        assert!(msg.contains("2 found"));
    }

    #[tokio::test]
    async fn view_tasks_worker_sees_own_task_and_the_unassigned_pool_but_not_a_foreign_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "mine", Some("alice"), "bob", "pending");
            seed(&guard, "pool", None, "bob", "pending");
            seed(&guard, "foreign", Some("carol"), "bob", "pending");
            // Foreign-visibility is on by default -- turn it off to
            // isolate the "own + pool" rule from that separate axis.
            disallow_foreign_view(&guard);
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result =
            ViewTasksTool::call(Some(&worker("alice")), &Value::Null, &conn, NOW, &ctx).await;
        let msg = message_of(&result);
        assert!(msg.contains("Task mine"));
        assert!(msg.contains("Task pool"));
        assert!(!msg.contains("Task foreign"));
    }

    #[tokio::test]
    async fn view_tasks_non_admin_targeting_a_foreign_agent_is_denied_when_policy_is_off() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", Some("carol"), "bob", "pending");
            disallow_foreign_view(&guard);
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&worker("alice")),
            &serde_json::json!({"agent_id": "carol"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn view_tasks_summary_mode_omits_created_by_line() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"summary": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Assigned to: Unassigned"));
        assert!(!msg.contains("Created by:"));
    }

    #[tokio::test]
    async fn view_tasks_show_dependencies_renders_a_dependency_analysis_block() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"show_dependencies": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Dependency Analysis"));
        assert!(msg.contains("READY - Can proceed"));
    }

    #[tokio::test]
    async fn view_tasks_show_health_analysis_renders_a_health_block() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "completed");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"show_health_analysis": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Health Analysis"));
    }

    #[tokio::test]
    async fn view_tasks_start_after_skips_the_anchor_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "pending");
            seed(&guard, "t2", None, "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let full = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"created_by": "bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let full_msg = message_of(&full);
        let first_id = if full_msg.find("ID: t1").unwrap_or(usize::MAX)
            < full_msg.find("ID: t2").unwrap_or(usize::MAX)
        {
            "t1"
        } else {
            "t2"
        };
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"start_after": first_id, "created_by": "bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(!msg.contains(&format!("ID: {first_id}")));
        assert!(msg.contains("1 found"));
    }

    #[tokio::test]
    async fn view_tasks_limit_reports_a_total_line() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "pending");
            seed(&guard, "t2", None, "bob", "pending");
            seed(&guard, "t3", None, "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = ViewTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"limit": 1, "created_by": "bob"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Total: 3 (showing offset=0, limit=1)"));
    }

    // -- search_tasks -----------------------------------------------------

    #[tokio::test]
    async fn search_tasks_requires_a_query_or_a_filter() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result =
            SearchTasksTool::call(Some(&admin("bob")), &Value::Null, &conn, NOW, &ctx).await;
        assert!(matches!(result, ToolResult::Invalid { field: None, .. }));
    }

    #[tokio::test]
    async fn search_tasks_rejects_a_query_with_only_short_terms() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = SearchTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"search_query": "to a"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("search_query"))
        );
    }

    #[tokio::test]
    async fn search_tasks_filter_only_lists_by_status_sorted_by_updated_at_desc() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "old", None, "bob", "pending");
            task_repository::update_fields(
                &guard,
                "old",
                &TaskFields {
                    status: Some("pending"),
                    ..Default::default()
                },
                "2026-01-01T00:00:00Z",
            )
            .unwrap();
            seed(&guard, "new", None, "bob", "pending");
            task_repository::update_fields(
                &guard,
                "new",
                &TaskFields {
                    status: Some("pending"),
                    ..Default::default()
                },
                "2026-01-10T00:00:00Z",
            )
            .unwrap();
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = SearchTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"status_filter": "pending"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.find("ID: new").unwrap() < msg.find("ID: old").unwrap());
    }

    #[tokio::test]
    async fn search_tasks_scores_a_title_match_above_a_notes_only_match() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            ensure_root(&guard);
            task_repository::create(
                &guard,
                NewTask {
                    task_id: Some("title_hit"),
                    title: "widget refactor",
                    description: None,
                    assigned_to: None,
                    created_by: "bob",
                    status: "pending",
                    priority: "medium",
                    parent_task: Some("root_anchor"),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: None,
                    now: NOW,
                },
            )
            .unwrap();
            task_repository::create(
                &guard,
                NewTask {
                    task_id: Some("notes_hit"),
                    title: "unrelated",
                    description: None,
                    assigned_to: None,
                    created_by: "bob",
                    status: "pending",
                    priority: "medium",
                    parent_task: Some("root_anchor"),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: Some(&[conexus_db::task_repository::TaskNote {
                        timestamp: NOW.to_string(),
                        author: Some("bob".to_string()),
                        content: "mentions widget in passing".to_string(),
                    }]),
                    now: NOW,
                },
            )
            .unwrap();
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = SearchTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"search_query": "widget"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.find("ID: title_hit").unwrap() < msg.find("ID: notes_hit").unwrap());
    }

    #[tokio::test]
    async fn search_tasks_with_no_matches_reports_the_query() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "t1", None, "bob", "pending");
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = SearchTasksTool::call(
            Some(&admin("bob")),
            &serde_json::json!({"search_query": "nonexistent"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert_eq!(
            result,
            ToolResult::Ok {
                data: None,
                message: Some("No tasks found containing 'nonexistent'.".to_string()),
            }
        );
    }

    #[tokio::test]
    async fn search_tasks_worker_does_not_see_a_foreign_task_when_policy_is_off() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed(&guard, "mine", Some("alice"), "bob", "pending");
            seed(&guard, "foreign", Some("carol"), "bob", "pending");
            disallow_foreign_view(&guard);
        }
        let registry = WaiterRegistry::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry);
        let result = SearchTasksTool::call(
            Some(&worker("alice")),
            &serde_json::json!({"assigned": true}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("ID: mine"));
        assert!(!msg.contains("ID: foreign"));
    }
}
