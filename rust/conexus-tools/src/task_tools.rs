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
