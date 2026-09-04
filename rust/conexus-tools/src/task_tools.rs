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
use rusqlite::{Connection, OptionalExtension};
use std::collections::HashSet;

use std::sync::LazyLock;

use conexus_auth::{Requirement, Tool};
use conexus_core::capability::Capability;
use conexus_core::principal::Principal;
use conexus_core::task_ownership::can_access_task;
use conexus_db::agent_repository::AgentRepository;
use conexus_db::task_repository::{self, NewTask, TaskNote, TaskRow};
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

pub(crate) fn str_arg(arguments: &Value, key: &str) -> Option<String> {
    arguments
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
}

pub(crate) fn bool_arg(arguments: &Value, key: &str) -> bool {
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

// ======================================================================
// create_task (Phase D4, PR 4/8)
// ======================================================================
//
// The first mutating tool in this module -- the first candidate to
// close the Phase D3-flagged `WaiterRegistry::notify()` gap
// (`ctx.waiter_registry.notify(&assignee)` on a direct assignment).
// Port of `create_task_tool_impl` (E1's canonical create-a-task path).
//
// No new `UnitOfWork` primitive per decision 1 in the migration plan's
// Phase D4 section -- one inline `conn.unchecked_transaction()`, same
// shape every prior mutating tool in this crate uses.
//
// Deliberately preserved, not "fixed": `status = "unassigned"` for a
// task created with no assignee is Python's OWN literal value, not
// one of `TaskQueryEngine`'s `ACTIVE_STATUSES` ("in_progress"/
// "pending") the claimable-pool predicate (`is_claimable_task`)
// checks -- so a task created via `create_task` with no assignee does
// NOT currently surface in the `unassigned=true` claimable pool. This
// looks like a genuine pre-existing Python inconsistency, not
// something this port introduces; per this migration's "re-derive,
// don't silently fix" discipline (matching `view_tasks`'s
// `start_after` bug, decision 5), it is ported bit-for-bit rather than
// quietly changed to `"pending"`.

pub(crate) fn normalize_parent(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(s)) => {
            let trimmed = s.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_string())
        }
        _ => None,
    }
}

/// BL-2: maintain the parent's `child_tasks` back-reference mirror in
/// the SAME transaction as the child INSERT. No-ops when
/// `parent_task_id` is `None` or the parent row is absent (deleted
/// mid-transaction -- vanishingly unlikely under this crate's single
/// Mutex-guarded connection, kept for parity with Python's own
/// defensive no-op). `child_tasks` is NOT one of the
/// `trg_tasks_terminal_state_guard` trigger's guarded columns (only
/// status/priority/notes/title/description/assigned_to-reassign are),
/// so this update always succeeds regardless of the parent's status.
pub(crate) fn link_child_to_parent(
    conn: &Connection,
    parent_task_id: Option<&str>,
    child_task_id: &str,
    now: &str,
) -> Result<(), ()> {
    let Some(parent_id) = parent_task_id else {
        return Ok(());
    };
    let parent = match task_repository::get_by_id(conn, parent_id) {
        Ok(Some(p)) => p,
        Ok(None) => return Ok(()),
        Err(_) => return Err(()),
    };
    let mut children = parent.child_tasks.unwrap_or_default();
    if children.iter().any(|c| c == child_task_id) {
        return Ok(());
    }
    children.push(child_task_id.to_string());
    match task_repository::update_fields(
        conn,
        parent_id,
        &conexus_db::task_repository::TaskFields {
            child_tasks: conexus_db::scheduled_directive_repository::NullableUpdate::Set(children),
            ..Default::default()
        },
        now,
    ) {
        Ok(_) => Ok(()),
        Err(_) => Err(()),
    }
}

pub struct CreateTaskTool;

impl Tool for CreateTaskTool {
    const NAME: &'static str = "create_task";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksCreate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Create a single task, optionally assigned to an agent \
        and/or parented under an existing task. Operator-tier task creation with assignability \
        + capability-routing safety checks.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_title": {"type": "string", "description": "Title of the task to create (required)."},
            "task_description": {"type": "string", "description": "Free-text task description."},
            "priority": {"type": "string", "description": "Task priority (default: medium).", "enum": ["low","medium","high"], "default": "medium"},
            "assigned_to": {"type": "string", "description": "Agent id to assign the task to. Must be a live agent. Omit for an unassigned task."},
            "parent_task": {"type": "string", "description": "Existing task id to parent this task under. Must exist. Omit for a top-level task."}
        },
        "required": ["task_title"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            let principal = principal.expect("Cap-gated tool always has a resolved principal");

            // SECURITY (pentest R1-F5): `tasks.create` alone is not the
            // tier gate -- it's also in the worker capability bundle
            // (workers use `create_self_task`). This tool is
            // operator/manager-tier; `tasks.assign` is the same
            // is_admin_request predicate every other worker/operator
            // split in this module uses.
            if !principal.has_capability(Capability::TasksAssign) {
                return ToolResult::PermissionDenied {
                    reason: "create_task is operator/manager-tier task creation; workers must \
                        use create_self_task"
                        .to_string(),
                };
            }

            let title = arguments
                .get("task_title")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if title.is_empty() {
                return ToolResult::Invalid {
                    field: Some("task_title".to_string()),
                    message: "task_title is required".to_string(),
                };
            }
            let description = arguments
                .get("task_description")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let priority = arguments
                .get("priority")
                .and_then(Value::as_str)
                .unwrap_or("medium")
                .to_string();
            if !["low", "medium", "high"].contains(&priority.as_str()) {
                return ToolResult::Invalid {
                    field: Some("priority".to_string()),
                    message: format!(
                        "Invalid priority {priority:?}: must be one of 'low', 'medium', 'high'."
                    ),
                };
            }
            let assigned_to = arguments
                .get("assigned_to")
                .and_then(Value::as_str)
                .map(str::to_string);
            let parent_task = normalize_parent(arguments.get("parent_task"));

            let requesting_admin_id = principal.actor_label().to_string();
            // Preserved bit-for-bit -- see this section's own module
            // doc on the "unassigned" status quirk.
            let status = if assigned_to.is_some() {
                "pending"
            } else {
                "unassigned"
            };

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error creating task".to_string(),
                    }
                }
            };

            // PF-R32-1b: pre-validate parent existence BEFORE the
            // INSERT -- a well-formed but nonexistent parent would
            // otherwise trip the self-FK at INSERT.
            if let Some(parent_id) = &parent_task {
                let exists: bool = tx
                    .query_row(
                        "SELECT 1 FROM tasks WHERE task_id = ?1",
                        [parent_id.as_str()],
                        |_| Ok(true),
                    )
                    .optional()
                    .unwrap_or(None)
                    .unwrap_or(false);
                if !exists {
                    return ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: parent_id.clone(),
                        hint: None,
                    };
                }
            }

            // BL-R13-1: a directly-assigned task must target a LIVE
            // agent.
            if let Some(assignee) = &assigned_to {
                if !agent_assignable(&tx, assignee) {
                    return ToolResult::Invalid {
                        field: None,
                        message: format!(
                            "Cannot assign task to '{assignee}': agent does not exist or is \
                             terminated."
                        ),
                    };
                }
            }

            // R15-BL-1: single-root-task invariant, same guard every
            // other create path in this module runs.
            if parent_task.is_none() {
                if let Some(conflict) = single_root_conflict(&tx) {
                    return conflict;
                }
            }

            let new_task = conexus_db::task_repository::NewTask {
                task_id: None,
                title,
                description: Some(&description),
                assigned_to: assigned_to.as_deref(),
                created_by: &requesting_admin_id,
                status,
                priority: &priority,
                parent_task: parent_task.as_deref(),
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now,
            };
            let fresh = match task_repository::create(&tx, new_task) {
                Ok(row) => row,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error creating task".to_string(),
                    }
                }
            };
            let task_id = fresh.task_id.clone();

            // BL-R30-1: set the gaining agent's `current_task` on a
            // create-with-assignee (prior=None -> SETs only when idle).
            if let Some(assignee) = &assigned_to {
                if conexus_db::agent_repository::AgentRepository::reconcile_current_task_on_reassign(
                    &tx,
                    &task_id,
                    None,
                    Some(assignee.as_str()),
                    now,
                )
                .is_err()
                {
                    return ToolResult::Failed {
                        message: "Database error creating task".to_string(),
                    };
                }
            }

            if let Err(_e) = agent_action_repository::log_agent_action(
                &tx,
                &requesting_admin_id,
                "created_task",
                Some(&task_id),
                Some(&serde_json::json!({"title": title, "assigned_to": assigned_to})),
                now,
            ) {
                // Best-effort audit log -- matches Python's own
                // fire-and-forget semantics, same rationale as
                // view_tasks/search_tasks above.
            }

            if link_child_to_parent(&tx, parent_task.as_deref(), &task_id, now).is_err() {
                return ToolResult::Failed {
                    message: "Database error creating task".to_string(),
                };
            }

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error creating task".to_string(),
                };
            }

            // Emit-iff-commit: the wake fires only after the write is
            // durable. A direct assignee gets a targeted wake; an
            // unassigned task broadcasts to every active agent
            // (`notify()` is a no-op for an agent with no in-flight
            // waiter, matching Python's `notify_unassigned_task_appeared`
            // fan-out-to-everyone-active semantics).
            if let Some(assignee) = &assigned_to {
                ctx.waiter_registry.notify(assignee);
            } else if let Ok(active) =
                conexus_db::agent_repository::AgentRepository::list_active(&conn)
            {
                for agent in active {
                    ctx.waiter_registry.notify(&agent.agent_id);
                }
            }

            ToolResult::Ok {
                data: Some(serde_json::json!({"task_id": task_id})),
                message: Some(format!("Task '{title}' created successfully")),
            }
        })
    }
}

// ======================================================================
// update_task_status / update_task (Phase D4, PR 5/8 pt.2)
// ======================================================================
//
// Both tools route every mutation through `task_mutation_engine::
// update_single_task` -- the single source of truth that keeps them
// (and, later, `bulk_task_operations`, PR 8) from drifting apart, per
// Python's own BL-R26-1 rationale. `update_task_status` additionally
// supports bulk operation (task_ids), cascade-to-children, and
// dependency auto-advance; `update_task` is the thinner admin
// field-editor with its own `assigned_to`-clearing carve-out
// (SECURITY R12-F5, see `UpdateTaskTool` below).
//
// Post-commit wake: unlike Python's cache-then-DB-fallback
// `_wake_task_assignees`, this port always re-reads each mutated
// task's CURRENT `assigned_to` fresh from the DB after commit --
// there is no in-memory cache to prefer here, so there's also no
// staleness risk to guard against.

fn outcome_error_message(
    outcome: &crate::task_mutation_engine::UpdateSingleTaskOutcome,
    task_id: &str,
) -> String {
    use crate::task_mutation_engine::UpdateSingleTaskOutcome as Outcome;
    match outcome {
        Outcome::Applied(_) => unreachable!("callers only call this for a non-Applied outcome"),
        Outcome::NotFound => format!("Task '{task_id}' not found"),
        Outcome::Unauthorized(msg)
        | Outcome::InvalidTransition(msg)
        | Outcome::AssigneeInvalid(msg)
        | Outcome::DependencyCycle(msg)
        | Outcome::DependencyIncomplete(msg) => msg.clone(),
    }
}

pub(crate) fn str_array_arg(arguments: &Value, key: &str) -> Option<Vec<String>> {
    arguments.get(key).and_then(Value::as_array).map(|items| {
        items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect()
    })
}

const VALID_TASK_STATUSES: [&str; 5] =
    ["pending", "in_progress", "completed", "cancelled", "failed"];

/// One task's outcome from an `update_task_status` sweep -- the Rust
/// equivalent of Python's per-task `{"success": bool, ...}` dict,
/// flattened to what the response-building/wake code actually needs.
struct SingleTaskResult {
    task_id: String,
    applied: Option<crate::task_mutation_engine::TaskUpdateApplied>,
    error: Option<String>,
}

pub struct UpdateTaskStatusTool;

impl Tool for UpdateTaskStatusTool {
    const NAME: &'static str = "update_task_status";
    const REQUIRED: Requirement = Requirement::Policy {
        keys: &["config_allow_worker_update_own_status"],
        default: true,
    };
    const DESCRIPTION: &'static str = "Smart task status update tool with bulk operations, \
        dependency management, and cascade features. Supports single task or bulk updates \
        with intelligent automation. You must own (be assigned) the task to change its \
        status. If the task is unassigned (in the claimable pool), claim it first with \
        assign_task(task_ids=[...], agent_token=<your own token>), then retry the status \
        update.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "ID of the task to update (for single task operations)."},
            "task_ids": {"type": "array", "description": "List of task IDs for bulk operations (alternative to task_id).", "items": {"type": "string"}},
            "status": {"type": "string", "description": "New status for the task(s).", "enum": ["pending","in_progress","completed","cancelled","failed"]},
            "notes": {"type": "string", "description": "Optional notes about the status update to be appended."},
            "title": {"type": "string", "description": "(Admin Only) New title for the task."},
            "description": {"type": "string", "description": "(Admin Only) New description for the task."},
            "priority": {"type": "string", "description": "(Admin Only) New priority.", "enum": ["low","medium","high"]},
            "assigned_to": {"type": "string", "description": "(Admin Only) New agent ID to assign the task to."},
            "depends_on_tasks": {"type": "array", "description": "(Admin Only) New list of task IDs this task depends on.", "items": {"type": "string"}},
            "auto_update_dependencies": {"type": "boolean", "description": "Automatically advance dependent tasks when their dependencies are completed (default: true).", "default": true},
            "cascade_to_children": {"type": "boolean", "description": "Cascade status changes to child tasks (only for failed/cancelled states, default: false).", "default": false},
            "validate_dependencies": {"type": "boolean", "description": "Validate dependency constraints before updating (default: true).", "default": true}
        },
        "required": ["status"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            use crate::task_mutation_engine::{
                advance_dependents_after_completion, update_single_task, TaskEdit,
                UpdateSingleTaskOutcome,
            };

            let principal = principal.expect("Policy-gated tool always has a resolved principal");

            let task_id_single = str_arg(arguments, "task_id");
            let task_ids_bulk = str_array_arg(arguments, "task_ids").unwrap_or_default();
            let notes_content = str_arg(arguments, "notes");
            let new_title = str_arg(arguments, "title");
            let new_description = str_arg(arguments, "description");
            let new_priority = str_arg(arguments, "priority");
            let new_assigned_to = str_arg(arguments, "assigned_to");
            let new_depends_on_tasks = str_array_arg(arguments, "depends_on_tasks");
            let auto_update_dependencies = arguments
                .get("auto_update_dependencies")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let cascade_to_children = arguments
                .get("cascade_to_children")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let validate_dependencies = arguments
                .get("validate_dependencies")
                .and_then(Value::as_bool)
                .unwrap_or(true);

            let task_ids_to_process: Vec<String> = if !task_ids_bulk.is_empty() {
                task_ids_bulk
            } else if let Some(id) = &task_id_single {
                vec![id.clone()]
            } else {
                return ToolResult::Invalid {
                    field: None,
                    message: "Either task_id or task_ids is required.".to_string(),
                };
            };

            let Some(new_status) = str_arg(arguments, "status") else {
                return ToolResult::Invalid {
                    field: Some("status".to_string()),
                    message: "status is required.".to_string(),
                };
            };
            if !VALID_TASK_STATUSES.contains(&new_status.as_str()) {
                return ToolResult::Invalid {
                    field: Some("status".to_string()),
                    message: format!(
                        "Invalid status: {new_status}. Valid: {}",
                        VALID_TASK_STATUSES.join(", ")
                    ),
                };
            }

            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error updating tasks".to_string(),
                    }
                }
            };

            let mut results: Vec<SingleTaskResult> = Vec::new();
            let mut tasks_to_cascade: Vec<String> = Vec::new();

            for task_id in &task_ids_to_process {
                let edit = TaskEdit {
                    notes_content: notes_content.as_deref(),
                    new_title: new_title.as_deref(),
                    new_description: new_description.as_deref(),
                    new_priority: new_priority.as_deref(),
                    new_assigned_to: new_assigned_to.as_deref(),
                    new_depends_on_tasks: new_depends_on_tasks.as_deref(),
                    system_transition: false,
                    validate_dependencies,
                };
                let outcome = match update_single_task(
                    &tx,
                    task_id,
                    &new_status,
                    &requesting_agent_id,
                    is_admin_request,
                    &edit,
                    now,
                ) {
                    Ok(o) => o,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error updating tasks".to_string(),
                        }
                    }
                };
                match outcome {
                    UpdateSingleTaskOutcome::Applied(applied) => {
                        if cascade_to_children {
                            tasks_to_cascade.extend(applied.child_tasks.clone());
                        }
                        let mut log_details = serde_json::json!({
                            "status": new_status,
                            "old_status": applied.old_status,
                        });
                        if notes_content.is_some() {
                            log_details["notes_added"] = serde_json::json!(true);
                        }
                        if let Err(_e) = agent_action_repository::log_agent_action(
                            &tx,
                            &requesting_agent_id,
                            "update_task_status",
                            Some(task_id),
                            Some(&log_details),
                            now,
                        ) {
                            // Best-effort audit log -- same rationale as
                            // every other mutating tool in this module.
                        }
                        results.push(SingleTaskResult {
                            task_id: task_id.clone(),
                            applied: Some(applied),
                            error: None,
                        });
                    }
                    other => {
                        let error = outcome_error_message(&other, task_id);
                        results.push(SingleTaskResult {
                            task_id: task_id.clone(),
                            applied: None,
                            error: Some(error),
                        });
                    }
                }
            }

            // Phase 2: smart cascade to children -- only for the
            // blocking terminal states, only when the caller opted in.
            let mut cascade_results: Vec<SingleTaskResult> = Vec::new();
            if cascade_to_children
                && !tasks_to_cascade.is_empty()
                && matches!(new_status.as_str(), "cancelled" | "failed")
            {
                for child_id in &tasks_to_cascade {
                    let edit =
                        TaskEdit::status_only(Some("Auto-cascaded from parent task status change"));
                    match update_single_task(
                        &tx,
                        child_id,
                        &new_status,
                        &requesting_agent_id,
                        is_admin_request,
                        &edit,
                        now,
                    ) {
                        Ok(UpdateSingleTaskOutcome::Applied(applied)) => {
                            cascade_results.push(SingleTaskResult {
                                task_id: child_id.clone(),
                                applied: Some(applied),
                                error: None,
                            });
                        }
                        Ok(other) => {
                            let error = outcome_error_message(&other, child_id);
                            cascade_results.push(SingleTaskResult {
                                task_id: child_id.clone(),
                                applied: None,
                                error: Some(error),
                            });
                        }
                        Err(_) => {
                            return ToolResult::Failed {
                                message: "Database error updating tasks".to_string(),
                            }
                        }
                    }
                }
            }

            // Phase 3: dependency auto-advance -- shared with the
            // (future, PR 8) bulk path via the same engine function.
            let mut dependency_updates: Vec<crate::task_mutation_engine::TaskUpdateApplied> =
                Vec::new();
            if auto_update_dependencies && new_status == "completed" {
                for result in &results {
                    if let Some(applied) = &result.applied {
                        match advance_dependents_after_completion(
                            &tx,
                            &applied.task_id,
                            &requesting_agent_id,
                            is_admin_request,
                            now,
                        ) {
                            Ok(advanced) => dependency_updates.extend(advanced),
                            Err(_) => {
                                return ToolResult::Failed {
                                    message: "Database error updating tasks".to_string(),
                                }
                            }
                        }
                    }
                }
            }

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error updating tasks".to_string(),
                };
            }

            // Post-commit: wake every mutated task's CURRENT assignee.
            let mut mutated_ids: Vec<String> = results
                .iter()
                .chain(cascade_results.iter())
                .filter_map(|r| r.applied.as_ref().map(|a| a.task_id.clone()))
                .collect();
            mutated_ids.extend(dependency_updates.iter().map(|a| a.task_id.clone()));
            mutated_ids.sort();
            mutated_ids.dedup();
            let mut woken: HashSet<String> = HashSet::new();
            for tid in &mutated_ids {
                if let Ok(Some(row)) = task_repository::get_by_id(&conn, tid) {
                    if let Some(assignee) = row.assigned_to.filter(|a| !a.is_empty()) {
                        if woken.insert(assignee.clone()) {
                            ctx.waiter_registry.notify(&assignee);
                        }
                    }
                }
            }

            let successful: Vec<&SingleTaskResult> =
                results.iter().filter(|r| r.applied.is_some()).collect();
            let failed: Vec<&SingleTaskResult> =
                results.iter().filter(|r| r.applied.is_none()).collect();

            let mut response_parts: Vec<String> = Vec::new();
            if task_ids_to_process.len() == 1 {
                if let Some(first) = successful.first() {
                    response_parts.push(format!(
                        "Task {} status updated to {new_status}.",
                        first.task_id
                    ));
                } else if let Some(first_fail) = failed.first() {
                    response_parts.push(format!(
                        "Failed to update task: {}",
                        first_fail.error.as_deref().unwrap_or("unknown error")
                    ));
                }
            } else {
                response_parts.push(format!(
                    "Bulk update completed: {}/{} tasks updated.",
                    successful.len(),
                    task_ids_to_process.len()
                ));
                if !failed.is_empty() {
                    response_parts.push("Failed updates:".to_string());
                    for fail in failed.iter().take(3) {
                        response_parts.push(format!(
                            "  - {}",
                            fail.error.as_deref().unwrap_or("unknown error")
                        ));
                    }
                    if failed.len() > 3 {
                        response_parts
                            .push(format!("  ... and {} more failures", failed.len() - 3));
                    }
                }
            }
            if !cascade_results.is_empty() {
                let successful_cascades = cascade_results
                    .iter()
                    .filter(|r| r.applied.is_some())
                    .count();
                response_parts.push(format!("Cascaded to {successful_cascades} child tasks."));
            }
            if !dependency_updates.is_empty() {
                response_parts.push(format!(
                    "Auto-advanced {} dependent tasks.",
                    dependency_updates.len()
                ));
            }

            // Single-task case: an unauthorized-only failure surfaces
            // as PermissionDenied (wire text starts with "Unauthorized:");
            // bulk callers keep the aggregated response shape.
            if task_ids_to_process.len() == 1 && !failed.is_empty() && successful.is_empty() {
                let error_text = failed[0].error.as_deref().unwrap_or_default();
                let lower = error_text.to_lowercase();
                if lower.starts_with("unauthorized") {
                    return ToolResult::PermissionDenied {
                        reason: error_text
                            .strip_prefix("Unauthorized: ")
                            .unwrap_or(error_text)
                            .to_string(),
                    };
                }
                if lower.contains("not found") {
                    return ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: task_ids_to_process[0].clone(),
                        hint: None,
                    };
                }
            }

            ToolResult::Ok {
                data: None,
                message: Some(response_parts.join("\n")),
            }
        })
    }
}

pub struct UpdateTaskTool;

impl Tool for UpdateTaskTool {
    const NAME: &'static str = "update_task";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksAssign,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Admin/manager task-field editor: mutate title, \
        description, priority, assigned_to, and/or append a note, with status OPTIONAL \
        (unlike update_task_status, where it's required). Enforces the same terminal-sink / \
        assignability / capability-routing invariants update_task_status does.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "ID of the task to update."},
            "status": {"type": "string", "description": "New status (optional -- omit to leave unchanged).", "enum": ["pending","in_progress","completed","cancelled","failed"]},
            "title": {"type": "string", "description": "New title for the task."},
            "description": {"type": "string", "description": "New description for the task."},
            "priority": {"type": "string", "description": "New priority.", "enum": ["low","medium","high"]},
            "assigned_to": {"type": "string", "description": "New agent id to assign the task to, or 'unassigned' (or an empty string) to clear the assignment."},
            "notes": {"type": "string", "description": "A new note to append (append-only; blank is ignored)."}
        },
        "required": ["task_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            use crate::task_mutation_engine::{
                update_single_task, TaskEdit, UpdateSingleTaskOutcome,
            };

            let principal = principal.expect("Cap-gated tool always has a resolved principal");

            let Some(task_id) = str_arg(arguments, "task_id") else {
                return ToolResult::Invalid {
                    field: Some("task_id".to_string()),
                    message: "task_id is a required field.".to_string(),
                };
            };

            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let explicit_status = str_arg(arguments, "status");
            if let Some(status) = &explicit_status {
                if !VALID_TASK_STATUSES.contains(&status.as_str()) {
                    return ToolResult::Invalid {
                        field: Some("status".to_string()),
                        message: format!(
                            "Invalid status: {status}. Valid: {}",
                            VALID_TASK_STATUSES.join(", ")
                        ),
                    };
                }
            }

            // title/description accept an explicit empty string
            // (clears the field); priority/notes require a truthy
            // value (a blank Save is a no-op, not an error) -- mirrors
            // the pre-refactor REST route exactly.
            let new_title = arguments.get("title").and_then(Value::as_str);
            let new_description = arguments.get("description").and_then(Value::as_str);
            let new_priority = str_arg(arguments, "priority").filter(|p| !p.is_empty());
            let notes_content = str_arg(arguments, "notes")
                .map(|n| n.trim().to_string())
                .filter(|n| !n.is_empty());

            let assigned_to_present = arguments
                .as_object()
                .is_some_and(|o| o.contains_key("assigned_to"));
            let raw_assigned = arguments.get("assigned_to").and_then(Value::as_str);
            let clearing = assigned_to_present
                && raw_assigned.is_none_or(|v| matches!(v.trim(), "" | "unassigned"));
            let reassign_target = if assigned_to_present && !clearing {
                raw_assigned.map(str::trim).map(str::to_string)
            } else {
                None
            };

            let admin_fields_requested = explicit_status.is_some()
                || new_title.is_some()
                || new_description.is_some()
                || new_priority.is_some()
                || notes_content.is_some()
                || reassign_target.is_some();

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error updating task".to_string(),
                    }
                }
            };

            let Some(prior) = (match task_repository::get_by_id(&tx, &task_id) {
                Ok(row) => row,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error updating task".to_string(),
                    }
                }
            }) else {
                return ToolResult::NotFound {
                    resource: "task".to_string(),
                    identifier: task_id,
                    hint: None,
                };
            };
            let prior_status = prior.status.clone();
            let prior_assignee = prior.assigned_to.clone();

            let mut log_details = serde_json::json!({});
            let mut changed_fields: Vec<&str> = Vec::new();

            if admin_fields_requested {
                // No explicit status -> thread the CURRENT status
                // through unchanged so the terminal-sink guard still
                // gates every other admin field.
                let final_status = explicit_status.clone().unwrap_or(prior_status.clone());
                let edit = TaskEdit {
                    notes_content: notes_content.as_deref(),
                    new_title,
                    new_description,
                    new_priority: new_priority.as_deref(),
                    new_assigned_to: reassign_target.as_deref(),
                    new_depends_on_tasks: None,
                    system_transition: false,
                    validate_dependencies: true,
                };
                let outcome = match update_single_task(
                    &tx,
                    &task_id,
                    &final_status,
                    &requesting_agent_id,
                    true,
                    &edit,
                    now,
                ) {
                    Ok(o) => o,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error updating task".to_string(),
                        }
                    }
                };
                match outcome {
                    UpdateSingleTaskOutcome::Applied(_) => {}
                    UpdateSingleTaskOutcome::NotFound => {
                        return ToolResult::NotFound {
                            resource: "task".to_string(),
                            identifier: task_id,
                            hint: None,
                        };
                    }
                    UpdateSingleTaskOutcome::InvalidTransition(msg) => {
                        return ToolResult::Conflict { reason: msg };
                    }
                    UpdateSingleTaskOutcome::AssigneeInvalid(msg) => {
                        return ToolResult::Invalid {
                            field: Some("assigned_to".to_string()),
                            message: msg,
                        };
                    }
                    UpdateSingleTaskOutcome::Unauthorized(msg)
                    | UpdateSingleTaskOutcome::DependencyCycle(msg)
                    | UpdateSingleTaskOutcome::DependencyIncomplete(msg) => {
                        return ToolResult::Failed { message: msg };
                    }
                }

                if let Some(status) = &explicit_status {
                    log_details["status_updated_to"] = serde_json::json!(status);
                    changed_fields.push("status");
                }
                if new_title.is_some() {
                    log_details["title_changed"] = serde_json::json!(true);
                    changed_fields.push("title");
                }
                if new_description.is_some() {
                    log_details["description_changed"] = serde_json::json!(true);
                    changed_fields.push("description");
                }
                if new_priority.is_some() {
                    log_details["priority_changed"] = serde_json::json!(true);
                    changed_fields.push("priority");
                }
                if notes_content.is_some() {
                    log_details["notes_added"] = serde_json::json!(true);
                    changed_fields.push("notes");
                }
                if let Some(target) = &reassign_target {
                    log_details["assigned_to_changed"] = serde_json::json!(target);
                    changed_fields.push("assigned_to");
                }
            }

            let mut clearing_fanout_needed = false;
            if clearing {
                // SECURITY (R12-F5): a bare `assigned_to: null` clear
                // never routes through `update_single_task` above
                // (admin_fields_requested is false for a clear-only
                // call), so it needs the SAME terminal-sink guard
                // applied explicitly here.
                if TERMINAL_TASK_STATUSES.contains(&prior_status.as_str()) {
                    return ToolResult::Conflict {
                        reason: format!(
                            "Cannot clear assignment for task '{task_id}': status \
                             '{prior_status}' is terminal (completed/cancelled/failed) and \
                             its assignment is frozen."
                        ),
                    };
                }
                let effective_status = explicit_status.clone().unwrap_or(prior_status.clone());
                let mut clear_fields = conexus_db::task_repository::TaskFields {
                    assigned_to: conexus_db::scheduled_directive_repository::NullableUpdate::Clear,
                    ..Default::default()
                };
                if !TERMINAL_TASK_STATUSES.contains(&effective_status.as_str()) {
                    clearing_fanout_needed = true;
                    if explicit_status.is_none() {
                        clear_fields.status = Some("unassigned");
                    }
                }
                if let Err(e) = task_repository::update_fields(&tx, &task_id, &clear_fields, now) {
                    return match e {
                        conexus_db::task_repository::UpdateTaskError::TerminalTaskWriteBlocked(
                            _,
                        ) => ToolResult::Conflict {
                            reason: format!(
                                "Cannot clear assignment for task '{task_id}': status \
                                     '{prior_status}' is terminal (completed/cancelled/failed) \
                                     and its assignment is frozen."
                            ),
                        },
                        conexus_db::task_repository::UpdateTaskError::Db(_) => ToolResult::Failed {
                            message: "Database error updating task".to_string(),
                        },
                    };
                }
                // BL-R30-1: `update_single_task` only reconciles on the
                // REASSIGN branch; clearing never reaches it.
                if let Err(_e) =
                    conexus_db::agent_repository::AgentRepository::reconcile_current_task_on_reassign(
                        &tx,
                        &task_id,
                        prior_assignee.as_deref(),
                        None,
                        now,
                    )
                {
                    return ToolResult::Failed {
                        message: "Database error updating task".to_string(),
                    };
                }
                log_details["assigned_to_changed"] = serde_json::Value::Null;
                changed_fields.push("assigned_to");
            }

            // Mirror the pre-refactor route: audit unconditionally
            // (even a request whose only supplied field was itself a
            // no-op), then only register write-observable effects when
            // something actually landed.
            if let Err(_e) = agent_action_repository::log_agent_action(
                &tx,
                &requesting_agent_id,
                "updated_task_dashboard",
                Some(&task_id),
                Some(&log_details),
                now,
            ) {
                // Best-effort audit log, same rationale as above.
            }

            if !(admin_fields_requested || clearing) {
                if tx.commit().is_err() {
                    return ToolResult::Failed {
                        message: "Database error updating task".to_string(),
                    };
                }
                return ToolResult::Ok {
                    data: None,
                    message: Some("Task updated successfully.".to_string()),
                };
            }

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error updating task".to_string(),
                };
            }

            // Post-commit: wake the touched assignee(s). A reassign/
            // clear wakes both the new AND prior assignee; a pure
            // field edit (no assignment change) wakes only the
            // current assignee.
            let reassigned = reassign_target.is_some() || clearing;
            let current_assignee: Option<String> = if let Some(target) = &reassign_target {
                Some(target.clone())
            } else if clearing {
                None
            } else {
                prior_assignee.clone()
            };
            let mut to_wake: Vec<String> = Vec::new();
            if let Some(a) = &current_assignee {
                to_wake.push(a.clone());
            }
            if reassigned {
                if let Some(prior) = &prior_assignee {
                    if Some(prior) != current_assignee.as_ref() {
                        to_wake.push(prior.clone());
                    }
                }
            }
            let mut woken: HashSet<String> = HashSet::new();
            for agent_id in to_wake {
                if !agent_id.is_empty() && woken.insert(agent_id.clone()) {
                    ctx.waiter_registry.notify(&agent_id);
                }
            }

            // BL-R16-1/BL-R17-1: unassigned-fanout parity -- only when
            // clearing landed a non-terminal task back to
            // 'unassigned'.
            if clearing_fanout_needed {
                if let Ok(active) =
                    conexus_db::agent_repository::AgentRepository::list_active(&conn)
                {
                    for agent in active {
                        ctx.waiter_registry.notify(&agent.agent_id);
                    }
                }
            }

            ToolResult::Ok {
                data: None,
                message: Some("Task updated successfully.".to_string()),
            }
        })
    }
}

// ======================================================================
// delete_task (Phase D4, PR 6/8)
// ======================================================================
//
// Operator-only, comprehensive-safety-check task deletion: refuses (as
// a Conflict) a task with live children/dependents/current-task
// pointers unless `force_delete` cascades through all three. Every
// cascade write lands in the SAME transaction as the root DELETE, so
// a mid-cascade failure rolls the whole thing back -- Rust gets this
// for free from `Transaction`'s own `Drop` (an un-committed
// transaction rolls back), which is why there's no Rust equivalent of
// Python's `_DeleteRolledBack` exception-for-control-flow class: just
// `return` before calling `.commit()`.

fn dependents_of(conn: &Connection, task_id: &str) -> rusqlite::Result<Vec<(String, Vec<String>)>> {
    let pattern = format!("%\"{task_id}\"%");
    let mut stmt = conn.prepare(
        "SELECT task_id, depends_on_tasks FROM tasks \
         WHERE json_extract(depends_on_tasks, '$') LIKE ?1",
    )?;
    let rows = stmt.query_map([pattern.as_str()], |row| {
        let deps_json: Option<String> = row.get(1)?;
        Ok((row.get::<_, String>(0)?, deps_json))
    })?;
    let mut out = Vec::new();
    for row in rows {
        let (id, deps_json) = row?;
        let deps: Vec<String> = deps_json
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();
        out.push((id, deps));
    }
    Ok(out)
}

pub struct DeleteTaskTool;

impl Tool for DeleteTaskTool {
    const NAME: &'static str = "delete_task";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksDelete,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Delete a task permanently with cascade handling for \
        related tasks. Operator-only operation with comprehensive safety checks.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "ID of the task to delete."},
            "force_delete": {"type": "boolean", "description": "Cascade delete children, dependents, and current-task pointers instead of refusing (default: false).", "default": false}
        },
        "required": ["task_id"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        _principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            use crate::task_mutation_engine::{
                update_single_task, TaskEdit, UpdateSingleTaskOutcome,
            };

            let Some(task_id) = str_arg(arguments, "task_id") else {
                return ToolResult::Invalid {
                    field: Some("task_id".to_string()),
                    message: "task_id is required".to_string(),
                };
            };
            let force_delete = arguments
                .get("force_delete")
                .and_then(Value::as_bool)
                .unwrap_or(false);

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            };

            let task_data = match task_repository::get_by_id(&tx, &task_id) {
                Ok(Some(row)) => row,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: task_id,
                        hint: None,
                    }
                }
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            };

            // BL-2: enumerate children authoritatively from the
            // `parent_task` FK column -- the source of truth, not the
            // `child_tasks` JSON mirror (which can drift).
            let direct_child_ids: Vec<String> = match tx
                .prepare("SELECT task_id FROM tasks WHERE parent_task = ?1")
                .and_then(|mut stmt| {
                    stmt.query_map([task_id.as_str()], |row| row.get(0))?
                        .collect()
                }) {
                Ok(ids) => ids,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            };
            if !direct_child_ids.is_empty() && !force_delete {
                return ToolResult::Conflict {
                    reason: format!(
                        "Task '{task_id}' has {} child tasks: {direct_child_ids:?}. Use \
                         force_delete=true to cascade delete.",
                        direct_child_ids.len()
                    ),
                };
            }

            let dependent_tasks = match dependents_of(&tx, &task_id) {
                Ok(rows) => rows,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            };
            if !dependent_tasks.is_empty() && !force_delete {
                let dependent_list: Vec<String> = dependent_tasks
                    .iter()
                    .map(|(id, _)| {
                        let title = task_repository::get_by_id(&tx, id)
                            .ok()
                            .flatten()
                            .map(|t| t.title)
                            .unwrap_or_default();
                        format!("{id} ({title})")
                    })
                    .collect();
                return ToolResult::Conflict {
                    reason: format!(
                        "{} tasks depend on '{task_id}': {dependent_list:?}. Use \
                         force_delete=true to cascade delete.",
                        dependent_tasks.len()
                    ),
                };
            }

            // BL-3: the `agents.current_task -> tasks.task_id` FK.
            let agents_on_task: Vec<String> = match tx
                .prepare("SELECT agent_id FROM agents WHERE current_task = ?1")
                .and_then(|mut stmt| {
                    stmt.query_map([task_id.as_str()], |row| row.get(0))?
                        .collect()
                }) {
                Ok(ids) => ids,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            };
            if !agents_on_task.is_empty() && !force_delete {
                return ToolResult::Conflict {
                    reason: format!(
                        "Task '{task_id}' is the current task of {} agent(s): \
                         {agents_on_task:?}. Use force_delete=true to clear it and cascade \
                         delete.",
                        agents_on_task.len()
                    ),
                };
            }

            let mut cascade_operations: Vec<String> = Vec::new();
            let mut deleted_events: Vec<(String, Option<String>)> =
                vec![(task_id.clone(), task_data.assigned_to.clone())];

            // Parent mirror upkeep.
            if let Some(parent_id) = &task_data.parent_task {
                if let Ok(Some(parent)) = task_repository::get_by_id(&tx, parent_id) {
                    let mut children = parent.child_tasks.unwrap_or_default();
                    if let Some(pos) = children.iter().position(|c| c == &task_id) {
                        children.remove(pos);
                        let _ = task_repository::update_fields(
                            &tx,
                            parent_id,
                            &conexus_db::task_repository::TaskFields {
                                child_tasks:
                                    conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                                        children,
                                    ),
                                ..Default::default()
                            },
                            now,
                        );
                        cascade_operations.push(format!(
                            "Updated parent task '{parent_id}' to remove child reference"
                        ));
                    }
                }
            }

            // Force-cascade the whole subtree, deepest descendant
            // first (the authoritative FK order).
            let mut delete_set_ids: Vec<String> = vec![task_id.clone()];
            if force_delete {
                let descendants = match collect_task_descendants(&tx, &task_id) {
                    Ok(d) => d,
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error deleting task".to_string(),
                        }
                    }
                };
                delete_set_ids.extend(descendants.iter().map(|(id, _)| id.clone()));
                let delete_set_refs: Vec<&str> =
                    delete_set_ids.iter().map(String::as_str).collect();
                // BL-3: NULL every agent's current_task pointer anywhere
                // in the delete set BEFORE the DELETEs, or the FK aborts
                // the DELETE and force_delete fails to force.
                let _ = conexus_db::agent_repository::AgentRepository::clear_current_task_for_many(
                    &tx,
                    &delete_set_refs,
                    now,
                );
                for (descendant_id, descendant_assignee) in &descendants {
                    if task_repository::delete(&tx, descendant_id).unwrap_or(false) {
                        deleted_events.push((descendant_id.clone(), descendant_assignee.clone()));
                        cascade_operations.push(format!("Deleted child task '{descendant_id}'"));
                    }
                }
            }

            // BL-R19-1: reconcile dangling depends_on_tasks references
            // across the WHOLE deleted set (root + every cascade-deleted
            // descendant) -- not just the root, or an OUTSIDE task
            // depending on a cascade-deleted descendant keeps a
            // reference to a now-absent id and stalls forever.
            let mut deps_to_refresh: HashSet<String> = HashSet::new();
            if force_delete {
                let deleted_id_set: HashSet<&str> =
                    delete_set_ids.iter().map(String::as_str).collect();
                let mut affected_deps: std::collections::HashMap<String, Vec<String>> =
                    std::collections::HashMap::new();
                for deleted_id in &delete_set_ids {
                    let rows = match dependents_of(&tx, deleted_id) {
                        Ok(r) => r,
                        Err(_) => {
                            return ToolResult::Failed {
                                message: "Database error deleting task".to_string(),
                            }
                        }
                    };
                    for (dep_id, deps) in rows {
                        if deleted_id_set.contains(dep_id.as_str()) {
                            continue;
                        }
                        affected_deps.entry(dep_id).or_insert(deps);
                    }
                }
                let mut reeval_candidates: HashSet<String> = HashSet::new();
                for (dep_id, dep_dependencies) in &affected_deps {
                    let pruned: Vec<String> = dep_dependencies
                        .iter()
                        .filter(|d| !deleted_id_set.contains(d.as_str()))
                        .cloned()
                        .collect();
                    if &pruned != dep_dependencies {
                        let _ = task_repository::update_fields(
                            &tx,
                            dep_id,
                            &conexus_db::task_repository::TaskFields {
                                depends_on_tasks:
                                    conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                                        pruned,
                                    ),
                                ..Default::default()
                            },
                            now,
                        );
                        deps_to_refresh.insert(dep_id.clone());
                        reeval_candidates.insert(dep_id.clone());
                        cascade_operations.push(format!(
                            "Updated task '{dep_id}' to remove dependency on deleted task(s) \
                             in the '{task_id}' cascade"
                        ));
                    }
                }

                // BL-R19-1: re-evaluate each unblocked task -- deletion,
                // unlike completion, never triggers the auto-advance,
                // so a task whose last blocking dependency was deleted
                // would otherwise never progress on its own.
                for dep_id in &reeval_candidates {
                    let Ok(Some(row)) = task_repository::get_by_id(&tx, dep_id) else {
                        continue;
                    };
                    if row.status != "pending" {
                        continue;
                    }
                    let remaining = row.depends_on_tasks.unwrap_or_default();
                    let all_completed = remaining.iter().all(|rid| {
                        task_repository::get_by_id(&tx, rid)
                            .ok()
                            .flatten()
                            .is_some_and(|r| r.status == "completed")
                    });
                    if all_completed {
                        let edit = TaskEdit::status_only(Some(
                            "Auto-advanced: blocking dependency deleted",
                        ));
                        if let Ok(UpdateSingleTaskOutcome::Applied(_)) = update_single_task(
                            &tx,
                            dep_id,
                            "in_progress",
                            "admin",
                            true,
                            &edit,
                            now,
                        ) {
                            cascade_operations.push(format!(
                                "Auto-advanced task '{dep_id}' to in_progress (blocking \
                                 dependency deleted)"
                            ));
                        }
                    }
                }
            }
            let _ = deps_to_refresh; // no in-memory cache to refresh in this port

            match task_repository::delete(&tx, &task_id) {
                Ok(true) => {}
                Ok(false) => {
                    // The transaction drops here without a commit --
                    // every cascade write above rolls back with it.
                    return ToolResult::Failed {
                        message: format!("Failed to delete task '{task_id}'"),
                    };
                }
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error deleting task".to_string(),
                    }
                }
            }

            // BL-R4-1: prune each deleted task's RAG chunk in the SAME
            // transaction as the row delete -- the incremental indexer
            // never sweeps orphans, so a deleted task's chunk would
            // otherwise stay queryable via ask_project_rag forever.
            for (deleted_id, _) in &deleted_events {
                let _ = conexus_db::rag_repository::purge_source(&tx, "task", deleted_id);
            }

            if let Err(_e) = agent_action_repository::log_agent_action(
                &tx,
                "admin",
                "deleted_task",
                Some(&task_id),
                Some(&serde_json::json!({
                    "task_title": task_data.title,
                    "force_delete": force_delete,
                    "cascade_operations": cascade_operations,
                })),
                now,
            ) {
                // Best-effort audit log, same rationale as every other
                // mutating tool in this module.
            }

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error deleting task".to_string(),
                };
            }

            // Post-commit: wake every deleted task's assignee.
            for (_, assignee) in &deleted_events {
                if let Some(a) = assignee.as_deref().filter(|a| !a.is_empty()) {
                    ctx.waiter_registry.notify(a);
                }
            }

            let mut response_parts = vec![format!(
                "Task '{task_id}' ({}) deleted successfully.",
                task_data.title
            )];
            if !cascade_operations.is_empty() {
                response_parts.push("\nCascade Operations:".to_string());
                for op in &cascade_operations {
                    response_parts.push(format!("  \u{2022} {op}"));
                }
            }
            response_parts.push(format!("\nDeletion completed at: {now}"));

            ToolResult::Ok {
                data: None,
                message: Some(response_parts.join("\n")),
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

// ======================================================================
// request_assistance / bulk_task_operations (Phase D4, PR 8/8 -- the
// LAST PR of Phase D4)
// ======================================================================

pub struct RequestAssistanceTool;

impl Tool for RequestAssistanceTool {
    const NAME: &'static str = "request_assistance";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::CoordinationAssist,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Request assistance with a task. This creates a child \
        task assigned to 'None' and notifies admin. You must own (be assigned) the task; if \
        it is unassigned (in the claimable pool), claim it first with \
        assign_task(task_ids=[...], agent_token=<your own>).";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "ID of the task for which assistance is needed (parent task)."},
            "description": {"type": "string", "description": "Description of the assistance required."}
        },
        "required": ["task_id", "description"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            use crate::task_mutation_engine::{is_unassigned_owner, worker_ownership_deny};

            let principal = principal.expect("Cap-gated tool always has a resolved principal");

            let Some(parent_task_id) = str_arg(arguments, "task_id") else {
                return ToolResult::Invalid {
                    field: None,
                    message: "task_id (for parent) and description are required.".to_string(),
                };
            };
            let Some(description) = str_arg(arguments, "description") else {
                return ToolResult::Invalid {
                    field: None,
                    message: "task_id (for parent) and description are required.".to_string(),
                };
            };

            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error requesting assistance".to_string(),
                    }
                }
            };

            let parent = match task_repository::get_by_id(&tx, &parent_task_id) {
                Ok(Some(row)) => row,
                Ok(None) => {
                    return ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: parent_task_id,
                        hint: None,
                    }
                }
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error requesting assistance".to_string(),
                    }
                }
            };

            // AZ-R17-1: a FOREIGN-owned task collapses to the same
            // phantom NotFound a nonexistent task returns; an
            // UNASSIGNED task (no owner to hide) gets the actionable
            // claim-it-first guidance instead.
            if !can_access_task(
                parent.assigned_to.as_deref(),
                Some(parent.created_by.as_str()),
                Some(requesting_agent_id.as_str()),
                is_admin_request,
                false,
                false,
                false,
            ) {
                return if is_unassigned_owner(parent.assigned_to.as_deref()) {
                    ToolResult::PermissionDenied {
                        reason: worker_ownership_deny(
                            &parent_task_id,
                            parent.assigned_to.as_deref(),
                            "request assistance on it",
                        )
                        .strip_prefix("Unauthorized: ")
                        .unwrap_or_default()
                        .to_string(),
                    }
                } else {
                    ToolResult::NotFound {
                        resource: "task".to_string(),
                        identifier: parent_task_id,
                        hint: None,
                    }
                };
            }

            let child_task_id = task_repository::generate_task_id();
            let child_title = format!("Assistance for {parent_task_id}: {}", parent.title);

            if let Err(_e) = task_repository::create(
                &tx,
                NewTask {
                    task_id: Some(&child_task_id),
                    title: &child_title,
                    description: Some(&description),
                    assigned_to: None,
                    created_by: &requesting_agent_id,
                    status: "pending",
                    priority: "high",
                    parent_task: Some(&parent_task_id),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: None,
                    now,
                },
            ) {
                return ToolResult::Failed {
                    message: "Database error requesting assistance".to_string(),
                };
            }

            let mut parent_children = parent.child_tasks.clone().unwrap_or_default();
            parent_children.push(child_task_id.clone());
            let mut parent_notes = parent.notes.clone().unwrap_or_default();
            parent_notes.push(conexus_db::task_repository::TaskNote {
                timestamp: now.to_string(),
                author: Some(requesting_agent_id.clone()),
                content: format!(
                    "Requested assistance: {description}. Assistance task created: \
                     {child_task_id}"
                ),
            });
            if let Err(_e) = task_repository::update_fields(
                &tx,
                &parent_task_id,
                &conexus_db::task_repository::TaskFields {
                    child_tasks: conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                        parent_children,
                    ),
                    notes: conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                        parent_notes,
                    ),
                    ..Default::default()
                },
                now,
            ) {
                return ToolResult::Failed {
                    message: "Database error requesting assistance".to_string(),
                };
            }

            if let Err(_e) = agent_action_repository::log_agent_action(
                &tx,
                &requesting_agent_id,
                "request_assistance",
                Some(&parent_task_id),
                Some(&serde_json::json!({
                    "description": description,
                    "child_task_id": child_task_id,
                })),
                now,
            ) {
                // Best-effort audit log, same rationale as elsewhere.
            }

            // Notify admin via the internal messaging seam. Best-effort
            // -- matches Python's own try/except around this call
            // ("Don't fail the entire operation if messaging fails").
            let admin_message = format!(
                "\u{1F6A8} Assistance Request from {requesting_agent_id}\n\nTask: \
                 {parent_task_id} - {}\nDescription: {description}\n\nChild assistance task \
                 created: {child_task_id}",
                parent.title
            );
            let message_sent = crate::agent_messaging::send_agent_message(
                &tx,
                principal,
                "admin",
                &admin_message,
                "assistance_request",
                "high",
                now,
            )
            .unwrap_or(Some("send failed".to_string()))
            .is_none();

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error requesting assistance".to_string(),
                };
            }
            if message_sent {
                ctx.waiter_registry.notify("admin");
            }

            ToolResult::Ok {
                data: Some(serde_json::json!({
                    "parent_task_id": parent_task_id,
                    "child_task_id": child_task_id,
                })),
                message: Some(format!(
                    "Assistance requested for task {parent_task_id}. Child assistance task \
                     {child_task_id} created. Admin notified via direct message."
                )),
            }
        })
    }
}

/// One bulk operation's outcome -- always a human-readable line,
/// mirroring Python's `results: List[str]` (a per-op error is a
/// STRING appended to the list, never an early return, so one bad op
/// in a batch doesn't abort the rest).
struct BulkOpOutcome {
    line: String,
    mutated_task_id: Option<String>,
    completed_task_id: Option<String>,
}

fn bulk_op_error(i: usize, line: impl std::fmt::Display) -> BulkOpOutcome {
    BulkOpOutcome {
        line: format!("Operation {}: {line}", i + 1),
        mutated_task_id: None,
        completed_task_id: None,
    }
}

pub struct BulkTaskOperationsTool;

impl Tool for BulkTaskOperationsTool {
    const NAME: &'static str = "bulk_task_operations";
    const REQUIRED: Requirement = Requirement::Cap {
        cap: Capability::TasksUpdate,
        reason: None,
    };
    const DESCRIPTION: &'static str = "Perform multiple task operations in a single atomic \
        transaction. Supports update_status, update_priority, add_note, and reassign (admin \
        only) operations. Critical for efficient batch task management.";
    const SCHEMA: &'static str = r#"{
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "List of operations to perform",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["update_status","update_priority","add_note","reassign"]},
                        "task_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending","in_progress","completed","cancelled","failed"]},
                        "priority": {"type": "string", "enum": ["low","medium","high"]},
                        "content": {"type": "string"},
                        "notes": {"type": "string"},
                        "assigned_to": {"type": "string"}
                    },
                    "required": ["type","task_id"],
                    "additionalProperties": false
                },
                "minItems": 1
            }
        },
        "required": ["operations"],
        "additionalProperties": false
    }"#;

    fn call<'a>(
        principal: Option<&'a Principal>,
        arguments: &'a Value,
        conn: &'a AsyncMutex<Connection>,
        now: &'a str,
        ctx: &'a conexus_auth::ToolCallContext<'a>,
    ) -> conexus_auth::BoxFuture<'a, ToolResult> {
        Box::pin(async move {
            use crate::task_mutation_engine::{
                advance_dependents_after_completion, worker_ownership_deny,
            };

            let principal = principal.expect("Cap-gated tool always has a resolved principal");
            let is_admin_request = principal.has_capability(Capability::TasksAssign);
            let requesting_agent_id = principal
                .agent_id
                .clone()
                .or_else(|| principal.user_id.clone())
                .unwrap_or_else(|| "admin".to_string());

            let Some(operations) = arguments.get("operations").and_then(Value::as_array) else {
                return ToolResult::Invalid {
                    field: Some("operations".to_string()),
                    message: "operations list is required and must be a non-empty array"
                        .to_string(),
                };
            };
            if operations.is_empty() {
                return ToolResult::Invalid {
                    field: Some("operations".to_string()),
                    message: "operations list is required and must be a non-empty array"
                        .to_string(),
                };
            }

            let conn = conn.lock().await;
            let tx = match conn.unchecked_transaction() {
                Ok(tx) => tx,
                Err(_) => {
                    return ToolResult::Failed {
                        message: "Database error in bulk operations".to_string(),
                    }
                }
            };

            let mut outcomes: Vec<BulkOpOutcome> = Vec::new();

            for (i, op) in operations.iter().enumerate() {
                let Some(op_obj) = op.as_object() else {
                    outcomes.push(bulk_op_error(
                        i,
                        "Invalid operation format (must be object)",
                    ));
                    continue;
                };
                let op_type = op_obj.get("type").and_then(Value::as_str);
                let task_id = op_obj.get("task_id").and_then(Value::as_str);
                let (Some(op_type), Some(task_id)) = (op_type, task_id) else {
                    outcomes.push(bulk_op_error(
                        i,
                        "Missing required fields 'type' and 'task_id'",
                    ));
                    continue;
                };

                let task_data = match task_repository::get_by_id(&tx, task_id) {
                    Ok(Some(row)) => row,
                    Ok(None) => {
                        outcomes.push(bulk_op_error(i, format!("Task '{task_id}' not found")));
                        continue;
                    }
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error in bulk operations".to_string(),
                        }
                    }
                };

                if !can_access_task(
                    task_data.assigned_to.as_deref(),
                    Some(task_data.created_by.as_str()),
                    Some(requesting_agent_id.as_str()),
                    is_admin_request,
                    false,
                    false,
                    false,
                ) {
                    let deny = worker_ownership_deny(
                        task_id,
                        task_data.assigned_to.as_deref(),
                        "modify it",
                    );
                    outcomes.push(bulk_op_error(i, deny));
                    continue;
                }

                match op_type {
                    "update_status" => {
                        let Some(new_status) = op_obj.get("status").and_then(Value::as_str) else {
                            outcomes.push(bulk_op_error(
                                i,
                                "Missing 'status' for update_status operation",
                            ));
                            continue;
                        };
                        if !VALID_TASK_STATUSES.contains(&new_status) {
                            outcomes
                                .push(bulk_op_error(i, format!("Invalid status '{new_status}'")));
                            continue;
                        }
                        if !is_admin_request
                            && !project_settings_repository::get_bool(
                                &tx,
                                "config_allow_worker_update_own_status",
                                true,
                            )
                        {
                            outcomes.push(bulk_op_error(
                                i,
                                "worker status updates disabled by project policy \
                                 (config_allow_worker_update_own_status=false). Ask an \
                                 admin to enable it in dashboard Settings.",
                            ));
                            continue;
                        }
                        if !is_status_transition_allowed(Some(&task_data.status), new_status) {
                            outcomes.push(bulk_op_error(
                                i,
                                format!(
                                    "Invalid status transition '{}' -> '{new_status}' for \
                                     task '{task_id}'",
                                    task_data.status
                                ),
                            ));
                            continue;
                        }
                        let notes_content = op_obj.get("notes").and_then(Value::as_str);
                        let mut notes = task_data.notes.clone().unwrap_or_default();
                        if let Some(content) = notes_content.filter(|c| !c.is_empty()) {
                            notes.push(conexus_db::task_repository::TaskNote {
                                timestamp: now.to_string(),
                                author: Some(requesting_agent_id.clone()),
                                content: content.to_string(),
                            });
                        }
                        if let Err(_e) = task_repository::update_fields(
                            &tx,
                            task_id,
                            &conexus_db::task_repository::TaskFields {
                                status: Some(new_status),
                                notes:
                                    conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                                        notes,
                                    ),
                                ..Default::default()
                            },
                            now,
                        ) {
                            return ToolResult::Failed {
                                message: "Database error in bulk operations".to_string(),
                            };
                        }
                        if TERMINAL_TASK_STATUSES.contains(&new_status) {
                            let _ = AgentRepository::clear_current_task_for(&tx, task_id, now);
                        }
                        outcomes.push(BulkOpOutcome {
                            line: format!(
                                "Operation {}: Task '{task_id}' status updated to '{new_status}'",
                                i + 1
                            ),
                            mutated_task_id: Some(task_id.to_string()),
                            completed_task_id: (new_status == "completed")
                                .then(|| task_id.to_string()),
                        });
                    }
                    "update_priority" => {
                        if !is_admin_request {
                            outcomes.push(bulk_op_error(
                                i,
                                "priority is an operator/manager-only field and cannot be \
                                 set by a worker; ask a supervisor to reprioritise",
                            ));
                            continue;
                        }
                        let new_priority = op_obj.get("priority").and_then(Value::as_str);
                        let Some(new_priority) =
                            new_priority.filter(|p| matches!(*p, "low" | "medium" | "high"))
                        else {
                            outcomes.push(bulk_op_error(
                                i,
                                format!("Invalid priority '{}'", new_priority.unwrap_or_default()),
                            ));
                            continue;
                        };
                        if TERMINAL_TASK_STATUSES.contains(&task_data.status.as_str()) {
                            outcomes.push(bulk_op_error(
                                i,
                                format!(
                                    "cannot update priority for task '{task_id}' -- its \
                                     status '{}' is terminal (completed/cancelled/failed)",
                                    task_data.status
                                ),
                            ));
                            continue;
                        }
                        if let Err(_e) = task_repository::update_fields(
                            &tx,
                            task_id,
                            &conexus_db::task_repository::TaskFields {
                                priority: Some(new_priority),
                                ..Default::default()
                            },
                            now,
                        ) {
                            return ToolResult::Failed {
                                message: "Database error in bulk operations".to_string(),
                            };
                        }
                        outcomes.push(bulk_op_error(
                            i,
                            format!("Task '{task_id}' priority updated to '{new_priority}'"),
                        ));
                    }
                    "add_note" => {
                        let Some(content) = op_obj.get("content").and_then(Value::as_str) else {
                            outcomes
                                .push(bulk_op_error(i, "Missing 'content' for add_note operation"));
                            continue;
                        };
                        if TERMINAL_TASK_STATUSES.contains(&task_data.status.as_str()) {
                            outcomes.push(bulk_op_error(
                                i,
                                format!(
                                    "cannot add note to task '{task_id}' -- its status '{}' \
                                     is terminal (completed/cancelled/failed)",
                                    task_data.status
                                ),
                            ));
                            continue;
                        }
                        let mut notes = task_data.notes.clone().unwrap_or_default();
                        notes.push(conexus_db::task_repository::TaskNote {
                            timestamp: now.to_string(),
                            author: Some(requesting_agent_id.clone()),
                            content: content.to_string(),
                        });
                        if let Err(_e) = task_repository::update_fields(
                            &tx,
                            task_id,
                            &conexus_db::task_repository::TaskFields {
                                notes:
                                    conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                                        notes,
                                    ),
                                ..Default::default()
                            },
                            now,
                        ) {
                            return ToolResult::Failed {
                                message: "Database error in bulk operations".to_string(),
                            };
                        }
                        outcomes.push(bulk_op_error(i, format!("Note added to task '{task_id}'")));
                    }
                    "reassign" if is_admin_request => {
                        let Some(new_assigned_to) =
                            op_obj.get("assigned_to").and_then(Value::as_str)
                        else {
                            outcomes.push(bulk_op_error(
                                i,
                                "Missing 'assigned_to' for reassign operation",
                            ));
                            continue;
                        };
                        if TERMINAL_TASK_STATUSES.contains(&task_data.status.as_str()) {
                            outcomes.push(bulk_op_error(
                                i,
                                format!(
                                    "cannot reassign task '{task_id}' -- its status '{}' is \
                                     terminal (completed/cancelled/failed)",
                                    task_data.status
                                ),
                            ));
                            continue;
                        }
                        if !agent_assignable(&tx, new_assigned_to) {
                            outcomes.push(bulk_op_error(
                                i,
                                format!(
                                    "Cannot reassign task '{task_id}' to '{new_assigned_to}': \
                                     agent does not exist or is terminated"
                                ),
                            ));
                            continue;
                        }
                        if let Err(_e) = task_repository::update_fields(
                            &tx,
                            task_id,
                            &conexus_db::task_repository::TaskFields {
                                assigned_to:
                                    conexus_db::scheduled_directive_repository::NullableUpdate::Set(
                                        new_assigned_to.to_string(),
                                    ),
                                ..Default::default()
                            },
                            now,
                        ) {
                            return ToolResult::Failed {
                                message: "Database error in bulk operations".to_string(),
                            };
                        }
                        let _ = AgentRepository::reconcile_current_task_on_reassign(
                            &tx,
                            task_id,
                            task_data.assigned_to.as_deref(),
                            Some(new_assigned_to),
                            now,
                        );
                        outcomes.push(BulkOpOutcome {
                            line: format!(
                                "Operation {}: Task '{task_id}' reassigned to '{new_assigned_to}'",
                                i + 1
                            ),
                            mutated_task_id: Some(task_id.to_string()),
                            completed_task_id: None,
                        });
                    }
                    "reassign" => {
                        outcomes.push(bulk_op_error(
                            i,
                            "reassigning a task to another agent is an operator/manager-only \
                             action; a worker cannot reassign -- ask a supervisor",
                        ));
                    }
                    other => {
                        outcomes.push(bulk_op_error(
                            i,
                            format!("Unknown operation type '{other}'"),
                        ));
                    }
                }
            }

            let mut mutated_task_ids: Vec<String> = outcomes
                .iter()
                .filter_map(|o| o.mutated_task_id.clone())
                .collect();
            let completed_task_ids: Vec<String> = outcomes
                .iter()
                .filter_map(|o| o.completed_task_id.clone())
                .collect();

            for done_id in &completed_task_ids {
                match advance_dependents_after_completion(
                    &tx,
                    done_id,
                    &requesting_agent_id,
                    is_admin_request,
                    now,
                ) {
                    Ok(advanced) => {
                        mutated_task_ids.extend(advanced.into_iter().map(|a| a.task_id));
                    }
                    Err(_) => {
                        return ToolResult::Failed {
                            message: "Database error in bulk operations".to_string(),
                        }
                    }
                }
            }

            let success_count = outcomes
                .iter()
                .filter(|o| !o.line.contains("Error"))
                .count();
            if let Err(_e) = agent_action_repository::log_agent_action(
                &tx,
                &requesting_agent_id,
                "bulk_task_operations",
                None,
                Some(&serde_json::json!({
                    "operations_count": operations.len(),
                    "success_count": success_count,
                })),
                now,
            ) {
                // Best-effort audit log, same rationale as elsewhere.
            }

            if tx.commit().is_err() {
                return ToolResult::Failed {
                    message: "Database error in bulk operations".to_string(),
                };
            }

            mutated_task_ids.sort();
            mutated_task_ids.dedup();
            let mut woken: HashSet<String> = HashSet::new();
            for tid in &mutated_task_ids {
                if let Ok(Some(row)) = task_repository::get_by_id(&conn, tid) {
                    if let Some(assignee) = row.assigned_to.filter(|a| !a.is_empty()) {
                        if woken.insert(assignee.clone()) {
                            ctx.waiter_registry.notify(&assignee);
                        }
                    }
                }
            }

            let response_text = format!(
                "Bulk Task Operations Results ({} operations):\n\n{}",
                operations.len(),
                outcomes
                    .iter()
                    .map(|o| o.line.as_str())
                    .collect::<Vec<_>>()
                    .join("\n")
            );

            ToolResult::Ok {
                data: None,
                message: Some(response_text),
            }
        })
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result =
            SearchTasksTool::call(Some(&admin("bob")), &Value::Null, &conn, NOW, &ctx).await;
        assert!(matches!(result, ToolResult::Invalid { field: None, .. }));
    }

    #[tokio::test]
    async fn search_tasks_rejects_a_query_with_only_short_terms() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
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

#[cfg(test)]
mod create_task_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::NewTask;
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-01-20T00:00:00Z";

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
            capabilities: Capabilities::from_iter([Capability::TasksCreate]),
        }
    }

    fn manager(agent_id: &str) -> Principal {
        Principal {
            kind: PrincipalKind::AgentBearer,
            user_id: None,
            agent_id: Some(agent_id.to_string()),
            project_name: None,
            project_role: None,
            agent_role: None,
            can_wake_loop: true,
            source_token: None,
            capabilities: Capabilities::from_iter([
                Capability::TasksCreate,
                Capability::TasksAssign,
            ]),
        }
    }

    async fn seed_agent(conn: &AsyncMutex<Connection>, agent_id: &str) {
        let guard = conn.lock().await;
        conexus_db::agent_repository::AgentRepository::create(
            &guard,
            conexus_db::agent_repository::NewAgent {
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

    fn seed_task(conn: &Connection, id: &str, parent: Option<&str>) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to: None,
                created_by: "bob",
                status: "pending",
                priority: "medium",
                parent_task: parent,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    fn task_id_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok {
                data: Some(data), ..
            } => data["task_id"].as_str().unwrap().to_string(),
            other => panic!("expected Ok with data, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn create_task_denies_a_plain_worker() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&worker("bob")),
            &serde_json::json!({"task_title": "x"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn create_task_requires_a_non_blank_title() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "   "}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("task_title"))
        );
    }

    #[tokio::test]
    async fn create_task_rejects_an_invalid_priority() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "x", "priority": "urgent"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("priority"))
        );
    }

    #[tokio::test]
    async fn create_task_rejects_a_nonexistent_parent() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "x", "parent_task": "ghost"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn create_task_rejects_assignment_to_a_dead_agent() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "x", "assigned_to": "ghost-agent"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Invalid { field: None, .. }));
    }

    #[tokio::test]
    async fn create_task_a_second_root_conflicts_with_the_existing_one() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", None);
        }
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "second root"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn create_task_unassigned_gets_the_preserved_unassigned_status() {
        // Pins the documented Python quirk: an unassigned task's status
        // is literally "unassigned", not "pending" -- ported as-is,
        // see this section's own module doc.
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "unassigned work"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let task_id = task_id_of(&result);
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, &task_id)
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "unassigned");
    }

    #[tokio::test]
    async fn create_task_assigned_gets_pending_status_and_sets_current_task() {
        let conn = test_conn();
        seed_agent(&conn, "carol").await;
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "for carol", "assigned_to": "carol"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let task_id = task_id_of(&result);
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, &task_id)
            .unwrap()
            .unwrap();
        assert_eq!(row.status, "pending");
        let agent = conexus_db::agent_repository::AgentRepository::get_by_id(&guard, "carol")
            .unwrap()
            .unwrap();
        assert_eq!(agent.current_task.as_deref(), Some(task_id.as_str()));
    }

    #[tokio::test]
    async fn create_task_links_the_new_task_into_the_parents_child_tasks() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", None);
        }
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "child", "parent_task": "root1"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let task_id = task_id_of(&result);
        let guard = conn.lock().await;
        let parent = task_repository::get_by_id(&guard, "root1")
            .unwrap()
            .unwrap();
        assert_eq!(parent.child_tasks, Some(vec![task_id]));
    }

    #[tokio::test]
    async fn create_task_writes_a_durable_audit_row() {
        let conn = test_conn();
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "audited"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        let task_id = task_id_of(&result);
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'created_task' AND \
                 task_id = ?1",
                [task_id.as_str()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn create_task_wakes_the_assignees_registered_waiter() {
        let conn = test_conn();
        seed_agent(&conn, "carol").await;
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("carol");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "for carol", "assigned_to": "carol"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }

    #[tokio::test]
    async fn create_task_unassigned_wakes_every_active_agents_waiter() {
        let conn = test_conn();
        seed_agent(&conn, "dave").await;
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("dave");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = CreateTaskTool::call(
            Some(&manager("alice")),
            &serde_json::json!({"task_title": "pool work"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }
}

#[cfg(test)]
mod update_task_status_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::scheduled_directive_repository::NullableUpdate;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{NewTask, TaskFields};
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-03-01T00:00:00Z";

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
            capabilities: Capabilities::from_iter([]),
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
            capabilities: Capabilities::from_iter([Capability::TasksAssign]),
        }
    }

    async fn seed_agent(conn: &AsyncMutex<Connection>, agent_id: &str) {
        let guard = conn.lock().await;
        AgentRepository::create(
            &guard,
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

    fn seed_task(
        conn: &Connection,
        id: &str,
        status: &str,
        assigned_to: Option<&str>,
        parent: Option<&str>,
    ) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by: "alice",
                status,
                priority: "medium",
                parent_task: parent,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    async fn call(principal: &Principal, args: Value, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        UpdateTaskStatusTool::call(Some(principal), &args, conn, NOW, &ctx).await
    }

    fn message_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn requires_task_id_or_task_ids() {
        let conn = test_conn();
        let result = call(&admin("a"), serde_json::json!({"status": "pending"}), &conn).await;
        assert!(matches!(result, ToolResult::Invalid { field: None, .. }));
    }

    #[tokio::test]
    async fn requires_status() {
        let conn = test_conn();
        let result = call(&admin("a"), serde_json::json!({"task_id": "t1"}), &conn).await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("status"))
        );
    }

    #[tokio::test]
    async fn rejects_an_invalid_status() {
        let conn = test_conn();
        let result = call(
            &admin("a"),
            serde_json::json!({"task_id": "t1", "status": "urgent"}),
            &conn,
        )
        .await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("status"))
        );
    }

    #[tokio::test]
    async fn worker_updates_own_task_status() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), None);
        }
        let result = call(
            &worker("bob"),
            serde_json::json!({"task_id": "t1", "status": "in_progress"}),
            &conn,
        )
        .await;
        assert_eq!(
            message_of(&result),
            "Task t1 status updated to in_progress."
        );
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.status, "in_progress");
    }

    #[tokio::test]
    async fn worker_on_a_foreign_task_gets_the_phantom_not_found() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("carol"), None);
        }
        let result = call(
            &worker("bob"),
            serde_json::json!({"task_id": "t1", "status": "in_progress"}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn worker_on_an_unassigned_task_gets_permission_denied() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None);
        }
        let result = call(
            &worker("bob"),
            serde_json::json!({"task_id": "t1", "status": "in_progress"}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn bulk_update_reports_partial_success() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None);
            seed_task(&guard, "t2", "completed", None, Some("t1"));
        }
        let result = call(
            &admin("a"),
            serde_json::json!({"task_ids": ["t1", "t2"], "status": "in_progress"}),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Bulk update completed: 1/2 tasks updated."));
        assert!(msg.contains("Failed updates:"));
    }

    #[tokio::test]
    async fn cascades_a_failed_status_to_children() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", "in_progress", None, None);
            seed_task(&guard, "child1", "in_progress", None, Some("root1"));
            // `seed_task` doesn't maintain the BL-2 child_tasks mirror
            // the real create_task tool writes -- set it explicitly so
            // this test reflects a realistic post-create_task state.
            task_repository::update_fields(
                &guard,
                "root1",
                &TaskFields {
                    child_tasks: NullableUpdate::Set(vec!["child1".to_string()]),
                    ..Default::default()
                },
                NOW,
            )
            .unwrap();
        }
        let result = call(
            &admin("a"),
            serde_json::json!({
                "task_id": "root1",
                "status": "failed",
                "cascade_to_children": true
            }),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Cascaded to 1 child tasks."));
        let guard = conn.lock().await;
        let child = task_repository::get_by_id(&guard, "child1")
            .unwrap()
            .unwrap();
        assert_eq!(child.status, "failed");
    }

    #[tokio::test]
    async fn auto_advances_a_now_unblocked_dependent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", "in_progress", None, None);
            seed_task(&guard, "dependent", "pending", None, Some("root1"));
            task_repository::update_fields(
                &guard,
                "dependent",
                &TaskFields {
                    depends_on_tasks: NullableUpdate::Set(vec!["root1".to_string()]),
                    ..Default::default()
                },
                NOW,
            )
            .unwrap();
        }
        let result = call(
            &admin("a"),
            serde_json::json!({"task_id": "root1", "status": "completed"}),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Auto-advanced 1 dependent tasks."));
        let guard = conn.lock().await;
        let dependent = task_repository::get_by_id(&guard, "dependent")
            .unwrap()
            .unwrap();
        assert_eq!(dependent.status, "in_progress");
    }

    #[tokio::test]
    async fn writes_a_per_task_audit_row() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None);
        }
        call(
            &admin("a"),
            serde_json::json!({"task_id": "t1", "status": "in_progress"}),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'update_task_status' \
                 AND task_id = 't1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn wakes_the_assignees_registered_waiter() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), None);
        }
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("bob");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = UpdateTaskStatusTool::call(
            Some(&worker("bob")),
            &serde_json::json!({"task_id": "t1", "status": "in_progress"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }
}

#[cfg(test)]
mod update_task_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::NewTask;
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-03-05T00:00:00Z";

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
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
            capabilities: Capabilities::from_iter([Capability::TasksAssign]),
        }
    }

    async fn seed_agent(conn: &AsyncMutex<Connection>, agent_id: &str) {
        let guard = conn.lock().await;
        AgentRepository::create(
            &guard,
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

    fn seed_task(conn: &Connection, id: &str, status: &str, assigned_to: Option<&str>) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by: "alice",
                status,
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

    async fn call(args: Value, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        UpdateTaskTool::call(Some(&admin("alice")), &args, conn, NOW, &ctx).await
    }

    fn message_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn requires_task_id() {
        let conn = test_conn();
        let result = call(serde_json::json!({}), &conn).await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("task_id"))
        );
    }

    #[tokio::test]
    async fn rejects_a_missing_task() {
        let conn = test_conn();
        let result = call(serde_json::json!({"task_id": "ghost"}), &conn).await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn updates_title_description_and_priority() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None);
        }
        let result = call(
            serde_json::json!({
                "task_id": "t1",
                "title": "new title",
                "description": "new description",
                "priority": "high"
            }),
            &conn,
        )
        .await;
        assert_eq!(message_of(&result), "Task updated successfully.");
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.title, "new title");
        assert_eq!(row.description.as_deref(), Some("new description"));
        assert_eq!(row.priority, "high");
    }

    #[tokio::test]
    async fn appends_a_note() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None);
        }
        call(
            serde_json::json!({"task_id": "t1", "notes": "  a note  "}),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        let notes = row.notes.unwrap();
        assert_eq!(notes.len(), 1);
        assert_eq!(notes[0].content, "a note");
    }

    #[tokio::test]
    async fn reassigning_sets_assigned_to_and_reconciles_current_task() {
        let conn = test_conn();
        seed_agent(&conn, "carol").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None);
        }
        call(
            serde_json::json!({"task_id": "t1", "assigned_to": "carol"}),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.assigned_to.as_deref(), Some("carol"));
        let agent = AgentRepository::get_by_id(&guard, "carol")
            .unwrap()
            .unwrap();
        assert_eq!(agent.current_task.as_deref(), Some("t1"));
    }

    #[tokio::test]
    async fn clearing_assignment_sets_status_unassigned_and_clears_current_task() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"));
            AgentRepository::reconcile_current_task_on_reassign(
                &guard,
                "t1",
                None,
                Some("bob"),
                NOW,
            )
            .unwrap();
        }
        call(
            serde_json::json!({"task_id": "t1", "assigned_to": "unassigned"}),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.assigned_to, None);
        assert_eq!(row.status, "unassigned");
        let agent = AgentRepository::get_by_id(&guard, "bob").unwrap().unwrap();
        assert_eq!(agent.current_task, None);
    }

    #[tokio::test]
    async fn clearing_a_terminal_tasks_assignment_is_a_conflict() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "completed", Some("bob"));
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "assigned_to": ""}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn a_call_with_nothing_to_change_is_a_no_op() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None);
        }
        let result = call(serde_json::json!({"task_id": "t1"}), &conn).await;
        assert_eq!(message_of(&result), "Task updated successfully.");
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row("SELECT COUNT(*) FROM agent_actions", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            count, 1,
            "still audited unconditionally, per the pre-refactor route"
        );
    }

    #[tokio::test]
    async fn reassigning_wakes_both_the_new_and_prior_assignees_waiters() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        seed_agent(&conn, "carol").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"));
        }
        let registry = WaiterRegistry::new();
        let (_tx_bob, mut rx_bob) = registry.register("bob");
        let (_tx_carol, mut rx_carol) = registry.register("carol");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = UpdateTaskTool::call(
            Some(&admin("alice")),
            &serde_json::json!({"task_id": "t1", "assigned_to": "carol"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx_bob.try_recv(), Ok(WakeSignal::Wake));
        assert_eq!(rx_carol.try_recv(), Ok(WakeSignal::Wake));
    }
}

#[cfg(test)]
mod delete_task_tests {
    use super::*;
    use conexus_db::agent_repository::{AgentRepository, NewAgent};
    use conexus_db::scheduled_directive_repository::NullableUpdate;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{NewTask, TaskFields};
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-03-10T00:00:00Z";

    fn test_conn() -> AsyncMutex<Connection> {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        AsyncMutex::new(conn)
    }

    async fn seed_agent(conn: &AsyncMutex<Connection>, agent_id: &str) {
        let guard = conn.lock().await;
        AgentRepository::create(
            &guard,
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
                created_by: "alice",
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

    async fn call(args: Value, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        DeleteTaskTool::call(None, &args, conn, NOW, &ctx).await
    }

    #[tokio::test]
    async fn requires_task_id() {
        let conn = test_conn();
        let result = call(serde_json::json!({}), &conn).await;
        assert!(
            matches!(result, ToolResult::Invalid { field, .. } if field.as_deref() == Some("task_id"))
        );
    }

    #[tokio::test]
    async fn rejects_a_missing_task() {
        let conn = test_conn();
        let result = call(serde_json::json!({"task_id": "ghost"}), &conn).await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn deletes_a_leaf_task_with_no_relations() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None, None);
        }
        let result = call(serde_json::json!({"task_id": "t1"}), &conn).await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        assert!(task_repository::get_by_id(&guard, "t1").unwrap().is_none());
    }

    #[tokio::test]
    async fn refuses_a_task_with_children_without_force() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", "pending", None, None, None);
            seed_task(&guard, "child1", "pending", None, Some("root1"), None);
        }
        let result = call(serde_json::json!({"task_id": "root1"}), &conn).await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
        let guard = conn.lock().await;
        assert!(task_repository::get_by_id(&guard, "root1")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn refuses_a_task_with_dependents_without_force() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None, None);
            seed_task(
                &guard,
                "t2",
                "pending",
                None,
                Some("t1"),
                Some(&["t1".to_string()]),
            );
        }
        let result = call(serde_json::json!({"task_id": "t1"}), &conn).await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn refuses_a_task_that_is_an_agents_current_task_without_force() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), None, None);
            AgentRepository::reconcile_current_task_on_reassign(
                &guard,
                "t1",
                None,
                Some("bob"),
                NOW,
            )
            .unwrap();
        }
        let result = call(serde_json::json!({"task_id": "t1"}), &conn).await;
        assert!(matches!(result, ToolResult::Conflict { .. }));
    }

    #[tokio::test]
    async fn force_delete_cascades_the_whole_subtree() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", "pending", None, None, None);
            seed_task(&guard, "child1", "pending", None, Some("root1"), None);
            seed_task(&guard, "grandchild1", "pending", None, Some("child1"), None);
        }
        let result = call(
            serde_json::json!({"task_id": "root1", "force_delete": true}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        assert!(task_repository::get_by_id(&guard, "root1")
            .unwrap()
            .is_none());
        assert!(task_repository::get_by_id(&guard, "child1")
            .unwrap()
            .is_none());
        assert!(task_repository::get_by_id(&guard, "grandchild1")
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn force_delete_clears_an_agents_current_task_pointer() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), None, None);
            AgentRepository::reconcile_current_task_on_reassign(
                &guard,
                "t1",
                None,
                Some("bob"),
                NOW,
            )
            .unwrap();
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "force_delete": true}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let agent = AgentRepository::get_by_id(&guard, "bob").unwrap().unwrap();
        assert_eq!(agent.current_task, None);
    }

    #[tokio::test]
    async fn force_delete_removes_the_parents_child_tasks_mirror_entry() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "root1", "pending", None, None, None);
            seed_task(&guard, "keeper", "pending", None, Some("root1"), None);
            seed_task(&guard, "goner", "pending", None, Some("root1"), None);
            task_repository::update_fields(
                &guard,
                "root1",
                &TaskFields {
                    child_tasks: NullableUpdate::Set(vec![
                        "keeper".to_string(),
                        "goner".to_string(),
                    ]),
                    ..Default::default()
                },
                NOW,
            )
            .unwrap();
        }
        let result = call(
            serde_json::json!({"task_id": "goner", "force_delete": true}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let parent = task_repository::get_by_id(&guard, "root1")
            .unwrap()
            .unwrap();
        assert_eq!(parent.child_tasks, Some(vec!["keeper".to_string()]));
    }

    #[tokio::test]
    async fn force_delete_prunes_a_dangling_dependency_reference() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            // "outside" is a SIBLING of "t1" under a shared root, not a
            // child of "t1" -- parent_task and depends_on_tasks are
            // independent relations, and conflating them would sweep
            // "outside" into t1's own descendant-cascade delete instead
            // of exercising the dangling-dependency-prune path.
            seed_task(&guard, "root1", "pending", None, None, None);
            seed_task(&guard, "t1", "pending", None, Some("root1"), None);
            seed_task(
                &guard,
                "outside",
                "pending",
                None,
                Some("root1"),
                Some(&["t1".to_string()]),
            );
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "force_delete": true}),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let outside = task_repository::get_by_id(&guard, "outside")
            .unwrap()
            .unwrap();
        assert_eq!(outside.depends_on_tasks, Some(vec![]));
    }

    #[tokio::test]
    async fn force_delete_auto_advances_a_dependent_left_with_no_remaining_deps() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            // Same independence-of-relations note as the dangling-
            // reference test above: "dependent" is a sibling of
            // "blocker" under a shared root, not its child.
            seed_task(&guard, "root1", "pending", None, None, None);
            seed_task(&guard, "blocker", "pending", None, Some("root1"), None);
            seed_task(
                &guard,
                "dependent",
                "pending",
                None,
                Some("root1"),
                Some(&["blocker".to_string()]),
            );
        }
        let result = call(
            serde_json::json!({"task_id": "blocker", "force_delete": true}),
            &conn,
        )
        .await;
        let msg = match &result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        };
        assert!(msg.contains("Auto-advanced task 'dependent' to in_progress"));
        let guard = conn.lock().await;
        let dependent = task_repository::get_by_id(&guard, "dependent")
            .unwrap()
            .unwrap();
        assert_eq!(dependent.status, "in_progress");
    }

    #[tokio::test]
    async fn writes_a_durable_audit_row() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", None, None, None);
        }
        call(serde_json::json!({"task_id": "t1"}), &conn).await;
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'deleted_task' AND \
                 task_id = 't1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn wakes_the_deleted_tasks_assignees_waiter() {
        let conn = test_conn();
        seed_agent(&conn, "bob").await;
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), None, None);
        }
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("bob");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = DeleteTaskTool::call(
            None,
            &serde_json::json!({"task_id": "t1"}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }
}

#[cfg(test)]
mod request_assistance_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::WaiterRegistry;

    const NOW: &str = "2026-05-10T00:00:00Z";

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
            capabilities: Capabilities::from_iter([Capability::CoordinationAssist]),
        }
    }

    fn seed_task(conn: &Connection, id: &str, assigned_to: Option<&str>, created_by: &str) {
        task_repository::create(
            conn,
            NewTask {
                task_id: Some(id),
                title: &format!("Task {id}"),
                description: None,
                assigned_to,
                created_by,
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

    async fn call(args: Value, principal: &Principal, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        RequestAssistanceTool::call(Some(principal), &args, conn, NOW, &ctx).await
    }

    #[tokio::test]
    async fn requires_task_id_and_description() {
        let conn = test_conn();
        let result = call(serde_json::json!({}), &worker("bob"), &conn).await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn rejects_a_missing_task() {
        let conn = test_conn();
        let result = call(
            serde_json::json!({"task_id": "ghost", "description": "help"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn worker_on_a_foreign_task_gets_the_phantom_not_found() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", Some("carol"), "alice");
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "description": "help"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::NotFound { .. }));
    }

    #[tokio::test]
    async fn worker_on_an_unassigned_task_gets_permission_denied() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", None, "alice");
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "description": "help"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::PermissionDenied { .. }));
    }

    #[tokio::test]
    async fn worker_on_own_task_creates_a_child_and_notifies_admin() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"task_id": "t1", "description": "stuck on X"}),
            &worker("bob"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let parent = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        let children = parent.child_tasks.unwrap();
        assert_eq!(children.len(), 1);
        let child = task_repository::get_by_id(&guard, &children[0])
            .unwrap()
            .unwrap();
        assert_eq!(child.priority, "high");
        assert_eq!(child.parent_task.as_deref(), Some("t1"));
        let msg_count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_messages WHERE recipient_id = 'admin'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(msg_count, 1);
    }

    #[tokio::test]
    async fn writes_a_durable_audit_row() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", Some("bob"), "alice");
        }
        call(
            serde_json::json!({"task_id": "t1", "description": "help"}),
            &worker("bob"),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'request_assistance'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}

#[cfg(test)]
mod bulk_task_operations_tests {
    use super::*;
    use conexus_core::capability::Capabilities;
    use conexus_core::principal::PrincipalKind;
    use conexus_db::agent_repository::NewAgent;
    use conexus_db::schema::init_schema;
    use conexus_wakeloop::waiter_registry::{WaiterRegistry, WakeSignal};

    const NOW: &str = "2026-05-15T00:00:00Z";

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
            capabilities: Capabilities::from_iter([Capability::TasksUpdate]),
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
            capabilities: Capabilities::from_iter([
                Capability::TasksUpdate,
                Capability::TasksAssign,
            ]),
        }
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

    fn seed_task(
        conn: &Connection,
        id: &str,
        status: &str,
        assigned_to: Option<&str>,
        created_by: &str,
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
                parent_task: None,
                child_tasks: None,
                depends_on_tasks: None,
                notes: None,
                now: NOW,
            },
        )
        .unwrap();
    }

    async fn call(args: Value, principal: &Principal, conn: &AsyncMutex<Connection>) -> ToolResult {
        let registry = WaiterRegistry::new();
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        BulkTaskOperationsTool::call(Some(principal), &args, conn, NOW, &ctx).await
    }

    fn message_of(result: &ToolResult) -> String {
        match result {
            ToolResult::Ok { message, .. } => message.clone().unwrap_or_default(),
            other => panic!("expected Ok, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn requires_a_non_empty_operations_array() {
        let conn = test_conn();
        let result = call(serde_json::json!({"operations": []}), &admin("a"), &conn).await;
        assert!(matches!(result, ToolResult::Invalid { .. }));
    }

    #[tokio::test]
    async fn update_status_op_succeeds_for_the_owner() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_status", "task_id": "t1", "status": "in_progress"}]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("status updated to 'in_progress'"));
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.status, "in_progress");
    }

    #[tokio::test]
    async fn update_status_on_a_foreign_task_is_reported_as_a_per_op_error() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("carol"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_status", "task_id": "t1", "status": "in_progress"}]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Task 't1' not found"));
    }

    #[tokio::test]
    async fn worker_cannot_update_priority() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_priority", "task_id": "t1", "priority": "high"}]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("operator/manager-only field"));
    }

    #[tokio::test]
    async fn admin_updates_priority() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_priority", "task_id": "t1", "priority": "high"}]}),
            &admin("alice"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("priority updated to 'high'"));
    }

    #[tokio::test]
    async fn add_note_appends_a_note() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        call(
            serde_json::json!({"operations": [{"type": "add_note", "task_id": "t1", "content": "progress"}]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.notes.unwrap()[0].content, "progress");
    }

    #[tokio::test]
    async fn worker_cannot_reassign() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "carol");
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "reassign", "task_id": "t1", "assigned_to": "carol"}]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("operator/manager-only"));
    }

    #[tokio::test]
    async fn admin_reassigns_and_reconciles_current_task() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "carol");
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "reassign", "task_id": "t1", "assigned_to": "carol"}]}),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let row = task_repository::get_by_id(&guard, "t1").unwrap().unwrap();
        assert_eq!(row.assigned_to.as_deref(), Some("carol"));
        let agent = AgentRepository::get_by_id(&guard, "carol")
            .unwrap()
            .unwrap();
        assert_eq!(agent.current_task.as_deref(), Some("t1"));
    }

    #[tokio::test]
    async fn refuses_a_terminal_task_status_transition() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "completed", Some("bob"), "alice");
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_status", "task_id": "t1", "status": "in_progress"}]}),
            &admin("alice"),
            &conn,
        )
        .await;
        let msg = message_of(&result);
        assert!(msg.contains("Invalid status transition"));
    }

    #[tokio::test]
    async fn completing_a_task_advances_a_now_unblocked_dependent() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "blocker", "in_progress", Some("bob"), "alice");
            task_repository::create(
                &guard,
                NewTask {
                    task_id: Some("dependent"),
                    title: "Task dependent",
                    description: None,
                    assigned_to: Some("bob"),
                    created_by: "alice",
                    status: "pending",
                    priority: "medium",
                    parent_task: Some("blocker"),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: None,
                    now: NOW,
                },
            )
            .unwrap();
            task_repository::update_fields(
                &guard,
                "dependent",
                &task_repository::TaskFields {
                    depends_on_tasks:
                        conexus_db::scheduled_directive_repository::NullableUpdate::Set(vec![
                            "blocker".to_string(),
                        ]),
                    ..Default::default()
                },
                NOW,
            )
            .unwrap();
        }
        let result = call(
            serde_json::json!({"operations": [{"type": "update_status", "task_id": "blocker", "status": "completed"}]}),
            &admin("alice"),
            &conn,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        let guard = conn.lock().await;
        let dependent = task_repository::get_by_id(&guard, "dependent")
            .unwrap()
            .unwrap();
        assert_eq!(dependent.status, "in_progress");
    }

    #[tokio::test]
    async fn writes_one_aggregate_audit_row() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
            task_repository::create(
                &guard,
                NewTask {
                    task_id: Some("t2"),
                    title: "Task t2",
                    description: None,
                    assigned_to: Some("bob"),
                    created_by: "alice",
                    status: "pending",
                    priority: "medium",
                    parent_task: Some("t1"),
                    child_tasks: None,
                    depends_on_tasks: None,
                    notes: None,
                    now: NOW,
                },
            )
            .unwrap();
        }
        call(
            serde_json::json!({"operations": [
                {"type": "add_note", "task_id": "t1", "content": "a"},
                {"type": "add_note", "task_id": "t2", "content": "b"}
            ]}),
            &worker("bob"),
            &conn,
        )
        .await;
        let guard = conn.lock().await;
        let count: i64 = guard
            .query_row(
                "SELECT COUNT(*) FROM agent_actions WHERE action_type = 'bulk_task_operations'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[tokio::test]
    async fn wakes_the_reassigned_agents_waiter() {
        let conn = test_conn();
        {
            let guard = conn.lock().await;
            seed_agent(&guard, "carol");
            seed_task(&guard, "t1", "pending", Some("bob"), "alice");
        }
        let registry = WaiterRegistry::new();
        let (_tx, mut rx) = registry.register("carol");
        let file_map = conexus_wakeloop::file_map::FileMap::new();
        let ctx = conexus_auth::ToolCallContext::off_wire(&registry, &file_map);
        let result = BulkTaskOperationsTool::call(
            Some(&admin("alice")),
            &serde_json::json!({"operations": [{"type": "reassign", "task_id": "t1", "assigned_to": "carol"}]}),
            &conn,
            NOW,
            &ctx,
        )
        .await;
        assert!(matches!(result, ToolResult::Ok { .. }));
        assert_eq!(rx.try_recv(), Ok(WakeSignal::Wake));
    }
}
