//! Port of `agent_mcp/features/task_queries.py`'s `TaskQueryEngine`
//! (Phase D4, PR 2/8) — the acknowledged genuinely-new-design slice
//! of the `task_tools.py` port (flagged in the Phase D4 research
//! pass), needed by exactly one tool (`view_tasks`, PR 3).
//! `search_tasks` (also PR 3) does NOT use this engine at all — it
//! reimplements its own filter/full-text logic independently in
//! Python, confirmed by grep before this port started.
//!
//! ## Re-derived, not ported at face value: SQL-backed, not an
//! in-memory global cache
//!
//! Python's engine is deliberately snapshot-based: it takes a
//! `task_source: Callable[[], Dict[task_id, row]]` (production passes
//! `lambda: g.tasks`, an in-memory dict kept in sync with the DB by a
//! separate write-side cache-upsert convention that has no Rust
//! equivalent anywhere in this migration — every other repository
//! ported so far goes straight to SQL, with no in-memory mirror). This
//! port takes a real `&Connection` and reads a fresh
//! `task_repository::list_all` snapshot per `query()` call instead —
//! strictly MORE correct than Python's `g.tasks` (which can only be as
//! fresh as its own upsert discipline), and consistent with this
//! workspace's own established repository pattern, not a new
//! architectural commitment. The FILTER/SORT/PAGINATE/HEALTH rules
//! themselves are ported bit-for-bit; only the snapshot's ORIGIN
//! changed.
//!
//! ## One deliberate, documented tie-break difference: `blocks_tasks`
//! ordering
//!
//! Python's reverse-dependency index (`health_of`'s "which tasks
//! depend on this one" loop) iterates a `dict`, whose insertion order
//! is incidental (CPython 3.7+ preserves it, but nothing about the
//! FEATURE depends on it — it's a display list, not a wire contract
//! another system parses positionally). This port's snapshot is a
//! `HashMap` (no ordering guarantee at all), so `blocks_tasks` is
//! explicitly sorted by task id here for deterministic, testable
//! output — a minor, intentional improvement over an incidental
//! Python ordering, not a preserved contract.

use std::collections::HashMap;

use conexus_core::task_ownership::can_access_task;
use conexus_db::pagination_cache::StableOrderCache;
use conexus_db::scheduled_directive_repository::parse_flexible;
use conexus_db::task_repository::{self, TaskRow};
use rusqlite::Connection;
use serde_json::{json, Value};

use crate::task_tools::status_filter_matches;

/// Statuses [`is_claimable_task`] and `health_metrics`'s "active ratio"
/// both key on. Port of `features/task_queries.py::_ACTIVE_STATUSES`.
const ACTIVE_STATUSES: [&str; 2] = ["in_progress", "pending"];

/// How many days without an update flags an active task "stale" in
/// `health_metrics`. Port of `_STALE_DAYS`.
const STALE_DAYS: i64 = 7;

fn priority_rank(priority: &str) -> i32 {
    match priority {
        "high" => 3,
        "medium" => 2,
        "low" => 1,
        _ => 2,
    }
}

fn status_rank(status: &str) -> i32 {
    match status {
        "failed" => 5,
        "in_progress" => 4,
        "pending" => 3,
        "completed" => 2,
        "cancelled" => 1,
        _ => 3,
    }
}

/// The ONE canonical "claimable/unassigned pool" predicate (R16-F2): a
/// task is claimable iff it is unassigned (`assigned_to` is NULL or
/// empty) AND its status is in [`ACTIVE_STATUSES`]. Applied at every
/// read surface so the read side can never drift from the write-side
/// terminal sink. Port of `is_claimable_task`.
pub fn is_claimable_task(task: &TaskRow) -> bool {
    let unassigned = task.assigned_to.as_deref().is_none_or(str::is_empty);
    unassigned && ACTIVE_STATUSES.contains(&task.status.as_str())
}

/// Declarative filter rules. All fields `None`/`false` mean "do not
/// filter on this dimension". Port of `TaskFilterSpec`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash)]
pub struct TaskFilterSpec {
    pub status: Option<String>,
    pub priority: Option<String>,
    pub agent_id: Option<String>,
    pub parent_task_id: Option<String>,
    pub blocked_only: bool,
    /// When `agent_id` is set, widens the match to the caller's own
    /// tasks together with the claimable pool -- the worker-facing
    /// "my tasks plus the claimable pool" visibility rule. Ignored
    /// when `agent_id` is `None`.
    pub include_unassigned: bool,
    pub created_by: Option<String>,
    /// Narrow to the unassigned (claimable) pool. A DEDICATED flag,
    /// never a magic `agent_id` sentinel value, so an agent literally
    /// named "unassigned" cannot collide with it.
    pub unassigned: bool,
    /// Complement of `unassigned`: narrow to tasks that HAVE an
    /// assignee. Setting both is contradictory and (by AND) matches
    /// nothing -- same as Python.
    pub assigned: bool,
}

/// Sort rule. All four keys sort DESCENDING -- that's the legacy
/// handler default and the test contract (Python's own
/// `_REVERSE_SORT_KEYS` happens to contain all four supported keys).
/// Port of `TaskSortSpec`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum SortBy {
    #[default]
    CreatedAt,
    UpdatedAt,
    Priority,
    Status,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub struct TaskSortSpec {
    pub by: SortBy,
}

/// Per-task dependency analysis result. Port of `TaskHealth`.
#[derive(Debug, Clone, PartialEq)]
pub struct TaskHealth {
    pub is_blocked: bool,
    pub can_start: bool,
    pub blocking_dependencies: Vec<String>,
    pub completed_dependencies: Vec<String>,
    pub missing_dependencies: Vec<String>,
    pub blocks_tasks: Vec<String>,
    pub dependency_health: String,
}

impl Default for TaskHealth {
    fn default() -> Self {
        // can_start defaults true / dependency_health defaults
        // "healthy" in Python -- NOT the derivable bool/String
        // defaults, so this can't be `#[derive(Default)]`.
        TaskHealth {
            is_blocked: false,
            can_start: true,
            blocking_dependencies: Vec::new(),
            completed_dependencies: Vec::new(),
            missing_dependencies: Vec::new(),
            blocks_tasks: Vec::new(),
            dependency_health: "healthy".to_string(),
        }
    }
}

impl TaskHealth {
    /// Render in the legacy `_analyze_task_dependencies` shape --
    /// `view_tasks_tool_impl`'s formatter reads these exact keys.
    pub fn as_json(&self) -> Value {
        json!({
            "is_blocked": self.is_blocked,
            "can_start": self.can_start,
            "blocking_dependencies": self.blocking_dependencies,
            "completed_dependencies": self.completed_dependencies,
            "missing_dependencies": self.missing_dependencies,
            "blocks_tasks": self.blocks_tasks,
            "dependency_health": self.dependency_health,
        })
    }
}

/// Compute the dependency analysis for a single task against a
/// snapshot of every task (needed for the reverse "who depends on me"
/// index). Port of `TaskQueryEngine.health_of`.
pub fn health_of(task: &TaskRow, all_tasks: &HashMap<String, TaskRow>) -> TaskHealth {
    let mut h = TaskHealth::default();
    let depends_on = task.depends_on_tasks.clone().unwrap_or_default();

    for dep_id in &depends_on {
        match all_tasks.get(dep_id) {
            Some(dep) => match dep.status.as_str() {
                "completed" => h.completed_dependencies.push(dep_id.clone()),
                "failed" | "cancelled" => {
                    h.blocking_dependencies.push(dep_id.clone());
                    h.is_blocked = true;
                    h.can_start = false;
                }
                "pending" | "in_progress" => {
                    h.blocking_dependencies.push(dep_id.clone());
                    if task.status == "pending" {
                        h.can_start = false;
                    }
                }
                _ => {}
            },
            None => {
                h.missing_dependencies.push(dep_id.clone());
                h.is_blocked = true;
                h.can_start = false;
            }
        }
    }

    // Reverse index: which tasks depend on this one. Sorted (see the
    // module doc's ordering note) rather than left in HashMap-
    // iteration order.
    for (other_id, other_task) in all_tasks {
        let other_deps = other_task.depends_on_tasks.as_deref().unwrap_or(&[]);
        if other_deps.iter().any(|d| d == &task.task_id) {
            h.blocks_tasks.push(other_id.clone());
        }
    }
    h.blocks_tasks.sort();

    // Roll up to a coarse grade for the adapter.
    if !h.missing_dependencies.is_empty() {
        h.dependency_health = "critical".to_string();
    } else if h.is_blocked && task.status == "in_progress" {
        h.dependency_health = "warning".to_string();
    } else if !h.can_start && task.status == "pending" {
        h.dependency_health = "waiting".to_string();
    }

    h
}

fn matches(task: &TaskRow, filters: &TaskFilterSpec, all_tasks: &HashMap<String, TaskRow>) -> bool {
    if let Some(status) = &filters.status {
        if !status_filter_matches(status, Some(task.status.as_str())) {
            return false;
        }
    }
    if let Some(priority) = &filters.priority {
        if &task.priority != priority {
            return false;
        }
    }
    if let Some(created_by) = &filters.created_by {
        if &task.created_by != created_by {
            return false;
        }
    }
    if filters.unassigned && !is_claimable_task(task) {
        // R16-F2: the "unassigned pool" is the CLAIMABLE pool -- a
        // terminal task the write side won't let anyone (re)claim is
        // not part of it.
        return false;
    }
    if filters.assigned && task.assigned_to.as_deref().is_none_or(str::is_empty) {
        return false;
    }
    if let Some(agent_id) = &filters.agent_id {
        let is_own = can_access_task(
            task.assigned_to.as_deref(),
            Some(task.created_by.as_str()),
            Some(agent_id.as_str()),
            false,
            false,
            false,
            false,
        );
        if filters.include_unassigned {
            // Worker pool visibility: own tasks OR the CLAIMABLE
            // (unassigned + non-terminal) pool. Own tasks stay visible
            // regardless of status; the widened pool applies the
            // R16-F2 terminal sink so it never advertises dead-end
            // work. Foreign-owned rows still fail -- cross-worker
            // isolation holds. Deliberately `is_claimable_task`, NOT
            // `can_access_task`'s own `include_unassigned` flag -- a
            // STRICTER "unassigned" than plain `is_unassigned`
            // (excludes terminal tasks too, R16-F2).
            if !is_own && !is_claimable_task(task) {
                return false;
            }
        } else if !is_own {
            return false;
        }
    }
    if let Some(parent_task_id) = &filters.parent_task_id {
        if task.parent_task.as_deref() != Some(parent_task_id.as_str()) {
            return false;
        }
    }
    if filters.blocked_only {
        let h = health_of(task, all_tasks);
        if !h.is_blocked && h.can_start {
            return false;
        }
    }
    true
}

/// Stable descending sort (see [`TaskSortSpec`]'s own doc on why every
/// key sorts this direction) -- a direct `sort_by` comparing in
/// reverse, NOT an ascending sort followed by `.reverse()`, since the
/// latter would also flip the relative order of TIED elements (Python's
/// `list.sort(reverse=True)` keeps ties in their original relative
/// order; Rust's `sort_by`/`sort_by_key` are stable, so comparing
/// `b` against `a` directly reproduces that exactly).
fn sort_tasks(tasks: &mut [TaskRow], sort: &TaskSortSpec) {
    match sort.by {
        SortBy::Priority => tasks.sort_by_key(|t| std::cmp::Reverse(priority_rank(&t.priority))),
        SortBy::Status => tasks.sort_by_key(|t| std::cmp::Reverse(status_rank(&t.status))),
        SortBy::UpdatedAt => tasks.sort_by(|a, b| b.updated_at.cmp(&a.updated_at)),
        SortBy::CreatedAt => tasks.sort_by(|a, b| b.created_at.cmp(&a.created_at)),
    }
}

/// Result of a [`TaskQueryEngine::query`] call. `tasks` is the
/// *window* after offset+limit slicing; `total_count` is the
/// matching-after-filter count BEFORE the window, so the caller can
/// render "Total: N" / "showing M of N". Port of `QueryResult`.
pub struct QueryResult {
    pub tasks: Vec<TaskRow>,
    pub total_count: usize,
}

/// Filter / sort / paginate / analyze tasks. Port of
/// `TaskQueryEngine` -- see the module doc for the SQL-backed
/// re-derivation from Python's in-memory-snapshot design.
pub struct TaskQueryEngine {
    pagination_cache: StableOrderCache<(TaskFilterSpec, TaskSortSpec), String>,
}

impl Default for TaskQueryEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskQueryEngine {
    pub fn new() -> Self {
        TaskQueryEngine {
            pagination_cache: StableOrderCache::default(),
        }
    }

    /// Run filter -> sort -> paginate against a fresh snapshot of the
    /// `tasks` table. `offset`/`limit` windowing goes through
    /// `pagination_cache` (R17-F2): an `offset == 0` call always
    /// re-filters/re-sorts fresh and anchors the resulting id
    /// ordering; a following `offset > 0` call for the SAME
    /// `(filters, sort)` shape replays that anchored ordering instead
    /// of re-filtering from scratch, so a task that leaves the matched
    /// set between two calls can no longer shift a still-matching task
    /// out of both pages.
    pub fn query(
        &self,
        conn: &Connection,
        filters: &TaskFilterSpec,
        sort: &TaskSortSpec,
        offset: i64,
        limit: Option<i64>,
    ) -> rusqlite::Result<QueryResult> {
        let snapshot: HashMap<String, TaskRow> = task_repository::list_all(conn, None)?
            .into_iter()
            .map(|t| (t.task_id.clone(), t))
            .collect();

        let key = (filters.clone(), *sort);
        let ordered_ids = self.pagination_cache.get_or_anchor(key, offset, || {
            let mut matched: Vec<TaskRow> = snapshot
                .values()
                .filter(|t| matches(t, filters, &snapshot))
                .cloned()
                .collect();
            sort_tasks(&mut matched, sort);
            Ok::<_, rusqlite::Error>(matched.into_iter().map(|t| t.task_id).collect())
        })?;

        // R21-F3: `ordered_ids` is the anchor frozen at sweep-start --
        // some may have been deleted outright since. Run the SAME
        // liveness check once, up front, and derive both
        // `total_count` and the window from that one filtered list.
        let live_ids: Vec<&String> = ordered_ids
            .iter()
            .filter(|tid| snapshot.contains_key(*tid))
            .collect();
        let total_count = live_ids.len();

        let offset_usize = offset.max(0) as usize;
        let mut window_ids: Vec<&String> = if offset > 0 {
            ordered_ids.iter().skip(offset_usize).collect()
        } else {
            ordered_ids.iter().collect()
        };
        if let Some(limit) = limit {
            window_ids.truncate(limit.max(0) as usize);
        }
        // Rows anchored earlier may since have been deleted outright
        // (as opposed to merely no longer matching `filters`) -- omit
        // them from the window rather than shifting a neighbour into
        // their place (that would reintroduce the original bug).
        let tasks = window_ids
            .into_iter()
            .filter_map(|tid| snapshot.get(tid).cloned())
            .collect();

        Ok(QueryResult { tasks, total_count })
    }

    /// Aggregate metrics over `tasks`. `now` is an explicit ISO-8601
    /// timestamp (this crate's established convention) rather than a
    /// hidden wall-clock read -- the one behavioral difference from
    /// Python's optional `now` injection, which defaults to a live
    /// clock read when the caller omits it; every real call site here
    /// already has a `now` to pass (see `agent_communication_tools.rs`'s
    /// own precedent for why `wait_for_events`'s slow-path loop is the
    /// ONE place in this workspace that reads the clock directly --
    /// this is not that place). Port of `TaskQueryEngine.health_metrics`.
    pub fn health_metrics(tasks: &[TaskRow], now: &str) -> Value {
        if tasks.is_empty() {
            return json!({"total": 0, "status": "no_data"});
        }
        let total = tasks.len();
        let mut status_counts: HashMap<String, i64> = HashMap::new();
        let mut priority_counts: HashMap<String, i64> = HashMap::new();
        let mut blocked_count: i64 = 0;
        let mut stale_count: i64 = 0;

        let current_time = parse_flexible(now).ok();

        for task in tasks {
            *status_counts.entry(task.status.clone()).or_insert(0) += 1;
            *priority_counts.entry(task.priority.clone()).or_insert(0) += 1;

            let deps = task.depends_on_tasks.as_deref().unwrap_or(&[]);
            if !deps.is_empty() && task.status == "pending" {
                blocked_count += 1;
            }

            if let (Some(current_time), Ok(updated_time)) =
                (current_time, parse_flexible(&task.updated_at))
            {
                let days_since_update = (current_time - updated_time).num_days();
                if days_since_update > STALE_DAYS && ACTIVE_STATUSES.contains(&task.status.as_str())
                {
                    stale_count += 1;
                }
            }
        }

        let total_f = total as f64;
        let completed_ratio = *status_counts.get("completed").unwrap_or(&0) as f64 / total_f;
        let active_ratio = (*status_counts.get("in_progress").unwrap_or(&0)
            + *status_counts.get("pending").unwrap_or(&0)) as f64
            / total_f;
        let blocked_ratio = blocked_count as f64 / total_f;
        let stale_ratio = stale_count as f64 / total_f;

        let health_score = (completed_ratio * 30.0
            + active_ratio * 40.0
            + (1.0 - blocked_ratio) * 20.0
            + (1.0 - stale_ratio) * 10.0)
            .clamp(0.0, 100.0);

        let health_status = if health_score >= 80.0 {
            "excellent"
        } else if health_score >= 60.0 {
            "good"
        } else if health_score >= 40.0 {
            "needs_attention"
        } else {
            "critical"
        };

        json!({
            "total": total,
            "status_distribution": status_counts,
            "priority_distribution": priority_counts,
            "blocked_tasks": blocked_count,
            "stale_tasks": stale_count,
            "health_score": (health_score * 10.0).round() / 10.0,
            "health_status": health_status,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use conexus_db::schema::init_schema;
    use conexus_db::task_repository::{create, NewTask};

    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        conn
    }

    #[allow(clippy::too_many_arguments)]
    fn new_task<'a>(
        id: &'a str,
        title: &'a str,
        status: &'a str,
        assigned_to: Option<&'a str>,
        parent: Option<&'a str>,
        created_by: &'a str,
        priority: &'a str,
        now: &'a str,
    ) -> NewTask<'a> {
        NewTask {
            task_id: Some(id),
            title,
            description: None,
            assigned_to,
            created_by,
            status,
            priority,
            parent_task: parent,
            child_tasks: None,
            depends_on_tasks: None,
            notes: None,
            now,
        }
    }

    // -- is_claimable_task ---------------------------------------------

    #[test]
    fn is_claimable_task_true_for_unassigned_active_task() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        assert!(is_claimable_task(&row));
    }

    #[test]
    fn is_claimable_task_false_when_assigned() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                Some("alice"),
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        assert!(!is_claimable_task(&row));
    }

    #[test]
    fn is_claimable_task_false_when_terminal() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "completed",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        let row = task_repository::get_by_id(&conn, "t1").unwrap().unwrap();
        assert!(!is_claimable_task(&row));
    }

    // -- health_of --------------------------------------------------------

    fn task_row(id: &str, status: &str, deps: Option<Vec<String>>) -> TaskRow {
        TaskRow {
            task_id: id.to_string(),
            title: id.to_string(),
            description: None,
            assigned_to: None,
            created_by: "bob".to_string(),
            status: status.to_string(),
            priority: "medium".to_string(),
            created_at: "2026-01-01T00:00:00Z".to_string(),
            updated_at: "2026-01-01T00:00:00Z".to_string(),
            parent_task: None,
            child_tasks: None,
            depends_on_tasks: deps,
            notes: None,
        }
    }

    #[test]
    fn health_of_no_deps_is_healthy_and_can_start() {
        let task = task_row("t1", "pending", None);
        let all = HashMap::from([("t1".to_string(), task.clone())]);
        let h = health_of(&task, &all);
        assert!(!h.is_blocked);
        assert!(h.can_start);
        assert_eq!(h.dependency_health, "healthy");
    }

    #[test]
    fn health_of_missing_dependency_is_critical_and_blocked() {
        let task = task_row("t1", "pending", Some(vec!["ghost".to_string()]));
        let all = HashMap::from([("t1".to_string(), task.clone())]);
        let h = health_of(&task, &all);
        assert!(h.is_blocked);
        assert!(!h.can_start);
        assert_eq!(h.missing_dependencies, vec!["ghost"]);
        assert_eq!(h.dependency_health, "critical");
    }

    #[test]
    fn health_of_failed_dependency_blocks_regardless_of_own_status() {
        let dep = task_row("dep", "failed", None);
        let task = task_row("t1", "in_progress", Some(vec!["dep".to_string()]));
        let all = HashMap::from([("t1".to_string(), task.clone()), ("dep".to_string(), dep)]);
        let h = health_of(&task, &all);
        assert!(h.is_blocked);
        assert!(!h.can_start);
        assert_eq!(h.blocking_dependencies, vec!["dep"]);
        assert_eq!(h.dependency_health, "warning"); // is_blocked && in_progress
    }

    #[test]
    fn health_of_pending_dependency_only_blocks_a_pending_dependent() {
        let dep = task_row("dep", "pending", None);
        let task = task_row("t1", "in_progress", Some(vec!["dep".to_string()]));
        let all = HashMap::from([("t1".to_string(), task.clone()), ("dep".to_string(), dep)]);
        let h = health_of(&task, &all);
        // Not blocked (in_progress dependent isn't gated the same way a
        // pending one is), but the pending dep still counts as
        // "blocking" for the list.
        assert!(h.can_start);
        assert_eq!(h.blocking_dependencies, vec!["dep"]);
    }

    #[test]
    fn health_of_completed_dependency_is_not_blocking() {
        let dep = task_row("dep", "completed", None);
        let task = task_row("t1", "pending", Some(vec!["dep".to_string()]));
        let all = HashMap::from([("t1".to_string(), task.clone()), ("dep".to_string(), dep)]);
        let h = health_of(&task, &all);
        assert!(!h.is_blocked);
        assert!(h.can_start);
        assert_eq!(h.completed_dependencies, vec!["dep"]);
    }

    #[test]
    fn health_of_reverse_index_finds_dependents_sorted() {
        let task = task_row("t1", "pending", None);
        let dependent_b = task_row("b_dependent", "pending", Some(vec!["t1".to_string()]));
        let dependent_a = task_row("a_dependent", "pending", Some(vec!["t1".to_string()]));
        let all = HashMap::from([
            ("t1".to_string(), task.clone()),
            ("b_dependent".to_string(), dependent_b),
            ("a_dependent".to_string(), dependent_a),
        ]);
        let h = health_of(&task, &all);
        assert_eq!(h.blocks_tasks, vec!["a_dependent", "b_dependent"]);
    }

    // -- TaskQueryEngine::query ---------------------------------------------

    #[test]
    fn query_filters_by_status() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "completed",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let filters = TaskFilterSpec {
            status: Some("completed".to_string()),
            ..Default::default()
        };
        let result = engine
            .query(&conn, &filters, &TaskSortSpec::default(), 0, None)
            .unwrap();
        assert_eq!(result.total_count, 1);
        assert_eq!(result.tasks[0].task_id, "t2");
    }

    #[test]
    fn query_incomplete_alias_matches_active_statuses() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "completed",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let filters = TaskFilterSpec {
            status: Some("incomplete".to_string()),
            ..Default::default()
        };
        let result = engine
            .query(&conn, &filters, &TaskSortSpec::default(), 0, None)
            .unwrap();
        assert_eq!(result.total_count, 1);
        assert_eq!(result.tasks[0].task_id, "t1");
    }

    #[test]
    fn query_agent_id_filter_scopes_to_own_tasks_only_by_default() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "mine",
                "pending",
                Some("alice"),
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "unassigned",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let filters = TaskFilterSpec {
            agent_id: Some("alice".to_string()),
            ..Default::default()
        };
        let result = engine
            .query(&conn, &filters, &TaskSortSpec::default(), 0, None)
            .unwrap();
        assert_eq!(result.total_count, 1);
        assert_eq!(result.tasks[0].task_id, "t1");
    }

    #[test]
    fn query_agent_id_with_include_unassigned_widens_to_the_claimable_pool() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "mine",
                "pending",
                Some("alice"),
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "pool",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t3",
                "foreign",
                "pending",
                Some("carol"),
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let filters = TaskFilterSpec {
            agent_id: Some("alice".to_string()),
            include_unassigned: true,
            ..Default::default()
        };
        let result = engine
            .query(&conn, &filters, &TaskSortSpec::default(), 0, None)
            .unwrap();
        let ids: std::collections::BTreeSet<_> =
            result.tasks.iter().map(|t| t.task_id.as_str()).collect();
        assert_eq!(ids, std::collections::BTreeSet::from(["t1", "t2"]));
    }

    #[test]
    fn query_unassigned_filter_excludes_terminal_tasks() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "completed",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let filters = TaskFilterSpec {
            unassigned: true,
            ..Default::default()
        };
        let result = engine
            .query(&conn, &filters, &TaskSortSpec::default(), 0, None)
            .unwrap();
        assert_eq!(result.total_count, 1);
        assert_eq!(result.tasks[0].task_id, "t1");
    }

    #[test]
    fn query_sorts_priority_descending() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "low",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "pending",
                None,
                Some("t1"),
                "bob",
                "high",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t3",
                "c",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:00Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let sort = TaskSortSpec {
            by: SortBy::Priority,
        };
        let result = engine
            .query(&conn, &TaskFilterSpec::default(), &sort, 0, None)
            .unwrap();
        let ids: Vec<&str> = result.tasks.iter().map(|t| t.task_id.as_str()).collect();
        assert_eq!(ids, vec!["t2", "t3", "t1"]); // high, medium, low
    }

    #[test]
    fn query_pagination_offset_and_limit() {
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:03Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:02Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t3",
                "c",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:01Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let sort = TaskSortSpec {
            by: SortBy::CreatedAt,
        }; // descending -> t1, t2, t3
        let page1 = engine
            .query(&conn, &TaskFilterSpec::default(), &sort, 0, Some(2))
            .unwrap();
        assert_eq!(page1.total_count, 3);
        assert_eq!(
            page1
                .tasks
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["t1", "t2"]
        );

        let page2 = engine
            .query(&conn, &TaskFilterSpec::default(), &sort, 2, Some(2))
            .unwrap();
        assert_eq!(
            page2
                .tasks
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["t3"]
        );
    }

    #[test]
    fn query_pagination_survives_a_deletion_between_pages() {
        // R17-F2/R21-F3: page 1 anchors an ordering; deleting a row
        // that already appeared on page 1 must not shift a later row
        // forward into a gap that spans both pages.
        let conn = test_conn();
        create(
            &conn,
            new_task(
                "t1",
                "a",
                "pending",
                None,
                None,
                "bob",
                "medium",
                "2026-01-01T00:00:04Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t2",
                "b",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:03Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t3",
                "c",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:02Z",
            ),
        )
        .unwrap();
        create(
            &conn,
            new_task(
                "t4",
                "d",
                "pending",
                None,
                Some("t1"),
                "bob",
                "medium",
                "2026-01-01T00:00:01Z",
            ),
        )
        .unwrap();

        let engine = TaskQueryEngine::new();
        let sort = TaskSortSpec {
            by: SortBy::CreatedAt,
        };
        let page1 = engine
            .query(&conn, &TaskFilterSpec::default(), &sort, 0, Some(2))
            .unwrap();
        assert_eq!(
            page1
                .tasks
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["t1", "t2"]
        );

        task_repository::delete(&conn, "t2").unwrap();

        let page2 = engine
            .query(&conn, &TaskFilterSpec::default(), &sort, 2, Some(2))
            .unwrap();
        // The anchored ordering still has t3 at index 2 -- t2's removal
        // does not shift t4 into page 1's territory.
        assert_eq!(
            page2
                .tasks
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["t3", "t4"]
        );
    }

    // -- health_metrics -------------------------------------------------

    #[test]
    fn health_metrics_empty_is_no_data() {
        assert_eq!(
            TaskQueryEngine::health_metrics(&[], "2026-01-01T00:00:00Z"),
            json!({"total": 0, "status": "no_data"})
        );
    }

    #[test]
    fn health_metrics_counts_statuses_and_priorities() {
        let tasks = vec![
            task_row("t1", "completed", None),
            task_row("t2", "pending", None),
        ];
        let metrics = TaskQueryEngine::health_metrics(&tasks, "2026-01-01T00:00:00Z");
        assert_eq!(metrics["total"], 2);
        assert_eq!(metrics["status_distribution"]["completed"], 1);
        assert_eq!(metrics["status_distribution"]["pending"], 1);
    }

    #[test]
    fn health_metrics_flags_stale_active_tasks() {
        let mut stale = task_row("t1", "in_progress", None);
        stale.updated_at = "2026-01-01T00:00:00Z".to_string();
        let metrics = TaskQueryEngine::health_metrics(&[stale], "2026-01-20T00:00:00Z");
        assert_eq!(metrics["stale_tasks"], 1);
    }

    #[test]
    fn health_metrics_score_is_clamped_and_categorized() {
        let tasks = vec![task_row("t1", "completed", None)];
        let metrics = TaskQueryEngine::health_metrics(&tasks, "2026-01-01T00:00:00Z");
        let score = metrics["health_score"].as_f64().unwrap();
        assert!((0.0..=100.0).contains(&score));
        assert!(metrics["health_status"].is_string());
    }
}
