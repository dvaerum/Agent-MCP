//! One rule for "does this requester own this task" (port of
//! `agent_mcp/core/task_ownership.py`).
//!
//! Python's module doc explains WHY this got consolidated: before it
//! existed, the rule was reimplemented independently at 5+ call sites
//! and had already drifted into three different shapes (`task_tools.py`:
//! exact `assigned_to == requester` only; `task_comments_tools.py`:
//! widened to also accept `created_by`; `task_queries.py` /
//! `features/rag/query.py`: widened to also accept an unassigned task).
//! Consolidating the CHECK itself — not the widenings, which are
//! genuinely different per call site and stay explicit, opt-in
//! parameters — means a future change to the base rule lands in one
//! place instead of requiring a class-sweep.
//!
//! [`can_access_task`] is the dict-based predicate in Python, for
//! callers that already have the task row (or a partial row with just
//! `assigned_to`/`created_by`) in hand — here it takes the two fields
//! directly rather than a `Mapping`, since every real Rust call site
//! already knows exactly which fields it has (no dict-shaped duck
//! typing needed the way Python's heterogeneous ORM-row/plain-dict
//! callers required). [`sql_fragment`] is the SQL-layer equivalent for
//! callers that scope a query at the DB layer instead of filtering an
//! already-fetched row (`rag/query.py`'s pre-vector-search task-context
//! fetch) — it only expresses the *exact-match* half of the rule (no
//! `include_created_by`/`include_unassigned` widening), since those
//! callers don't need it today.

/// Whether a task's `assigned_to` is NULL/empty/whitespace-only — the
/// write-path definition of "in the claimable pool, no owner to hide"
/// used by [`can_access_task`]'s `include_unassigned` widening (mirrors
/// Python's `task_tools._worker_ownership_deny`'s own
/// `assignee is None or assignee.strip() == ""` check).
///
/// NOT the same predicate as Python's `task_queries.is_claimable_task`,
/// which additionally excludes terminal-status tasks (the read-path's
/// stricter "advertise as claimable" rule) — that extra axis is
/// specific to the claimable-pool LISTING and has no port here; compose
/// it on top of this function wherever that listing itself gets ported,
/// rather than folding task-status semantics into this module.
pub fn is_unassigned(assigned_to: Option<&str>) -> bool {
    match assigned_to {
        None => true,
        Some(v) => v.trim().is_empty(),
    }
}

/// Whether `requester_id` may access a task with the given
/// `assigned_to`/`created_by` fields.
///
/// `can_view_all_tasks` is the caller's already-resolved `tasks.assign`
/// capability check (operator/manager/sysadmin) — this function does
/// not know about `Principal`; every call site threads that bool in
/// pre-resolved, exactly as Python's callers already do for
/// `is_admin_request`/`can_view_all_tasks` today.
///
/// `include_foreign` is the `config_allow_worker_view_foreign_tasks`/
/// `config_allow_worker_comment_foreign_tasks` widening (both default
/// `true` in Python's schema): a task assigned to a DIFFERENT agent is
/// accessible too. Deliberately distinct from `include_unassigned` — an
/// unassigned task has no owner to hide (already the claimable pool); a
/// foreign-owned task has a real owner this flag chooses to stop hiding
/// from. A caller wanting both passes both flags.
///
/// A missing `requester_id` degrades CLOSED: with no admin bypass and
/// `include_foreign` off, it can only ever match `include_unassigned`
/// (never an `assigned_to`/`created_by` equality, since `None` is
/// deliberately never treated as a wildcard).
#[allow(clippy::too_many_arguments)]
pub fn can_access_task(
    assigned_to: Option<&str>,
    created_by: Option<&str>,
    requester_id: Option<&str>,
    can_view_all_tasks: bool,
    include_created_by: bool,
    include_unassigned: bool,
    include_foreign: bool,
) -> bool {
    if can_view_all_tasks {
        return true;
    }
    if let Some(requester) = requester_id {
        if assigned_to == Some(requester) {
            return true;
        }
        if include_created_by && created_by == Some(requester) {
            return true;
        }
    }
    if include_unassigned && is_unassigned(assigned_to) {
        return true;
    }
    if include_foreign && !is_unassigned(assigned_to) {
        return true;
    }
    false
}

/// Return the `(sql_fragment, params)` that scopes a `tasks` `SELECT`
/// to [`can_access_task`]'s exact-match rule, optionally widened by
/// `include_foreign` (no `include_created_by`/`include_unassigned`
/// support — those callers don't need it today).
///
/// An admin/manager (`can_view_all_tasks`) gets an empty fragment
/// (unscoped). `include_foreign` scopes to "any task with a real
/// assignee" (own ∪ foreign — the exact complement of unassigned),
/// needing no `requester_id` at all. Otherwise: `AND assigned_to = ?`
/// bound to `requester_id`. A missing/falsy `requester_id` (with
/// `include_foreign` off) degrades closed via an unsatisfiable `AND
/// 1=0` — NOT `assigned_to = ''`, which would accidentally match a
/// real row whose `assigned_to` is itself the empty string
/// ([`is_unassigned`] treats `""` as unassigned, so such rows exist)
/// rather than matching nothing.
pub fn sql_fragment(
    requester_id: Option<&str>,
    can_view_all_tasks: bool,
    include_foreign: bool,
) -> (&'static str, Vec<String>) {
    if can_view_all_tasks {
        return ("", Vec::new());
    }
    if include_foreign {
        return (
            " AND assigned_to IS NOT NULL AND assigned_to != ''",
            Vec::new(),
        );
    }
    match requester_id {
        None | Some("") => (" AND 1=0", Vec::new()),
        Some(requester) => (" AND assigned_to = ?", vec![requester.to_string()]),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── is_unassigned ────────────────────────────────────────────────

    #[test]
    fn is_unassigned_true_for_none_empty_and_whitespace() {
        assert!(is_unassigned(None));
        assert!(is_unassigned(Some("")));
        assert!(is_unassigned(Some("   ")));
    }

    #[test]
    fn is_unassigned_false_for_a_real_agent_id() {
        assert!(!is_unassigned(Some("agent-1")));
    }

    // ── can_access_task ──────────────────────────────────────────────

    #[test]
    fn admin_bypasses_every_other_check() {
        assert!(can_access_task(
            Some("someone-else"),
            None,
            Some("requester"),
            true,
            false,
            false,
            false,
        ));
    }

    #[test]
    fn exact_assigned_to_match_is_always_admitted() {
        assert!(can_access_task(
            Some("requester"),
            None,
            Some("requester"),
            false,
            false,
            false,
            false,
        ));
    }

    #[test]
    fn a_foreign_assignee_is_denied_by_default() {
        assert!(!can_access_task(
            Some("someone-else"),
            None,
            Some("requester"),
            false,
            false,
            false,
            false,
        ));
    }

    #[test]
    fn created_by_match_requires_include_created_by() {
        let task = (Some("someone-else"), Some("requester"));
        assert!(!can_access_task(
            task.0,
            task.1,
            Some("requester"),
            false,
            false,
            false,
            false,
        ));
        assert!(can_access_task(
            task.0,
            task.1,
            Some("requester"),
            false,
            true,
            false,
            false,
        ));
    }

    #[test]
    fn unassigned_task_requires_include_unassigned() {
        assert!(!can_access_task(
            None,
            None,
            Some("requester"),
            false,
            false,
            false,
            false,
        ));
        assert!(can_access_task(
            None,
            None,
            Some("requester"),
            false,
            false,
            true,
            false,
        ));
    }

    #[test]
    fn empty_string_assigned_to_counts_as_unassigned_for_the_widening() {
        assert!(can_access_task(
            Some(""),
            None,
            Some("requester"),
            false,
            false,
            true,
            false,
        ));
    }

    #[test]
    fn foreign_widening_admits_any_real_assignee_but_not_unassigned() {
        assert!(can_access_task(
            Some("someone-else"),
            None,
            Some("requester"),
            false,
            false,
            false,
            true,
        ));
        assert!(!can_access_task(
            None,
            None,
            Some("requester"),
            false,
            false,
            false,
            true,
        ));
    }

    #[test]
    fn a_missing_requester_id_degrades_closed_except_via_unassigned() {
        assert!(!can_access_task(
            Some("someone"),
            Some("someone"),
            None,
            false,
            true,
            false,
            false,
        ));
        assert!(can_access_task(None, None, None, false, false, true, false,));
    }

    // ── sql_fragment ─────────────────────────────────────────────────

    #[test]
    fn admin_gets_an_unscoped_empty_fragment() {
        assert_eq!(sql_fragment(Some("requester"), true, false), ("", vec![]));
    }

    #[test]
    fn foreign_scopes_to_any_real_assignee_needing_no_requester() {
        assert_eq!(
            sql_fragment(None, false, true),
            (" AND assigned_to IS NOT NULL AND assigned_to != ''", vec![])
        );
    }

    #[test]
    fn exact_match_binds_the_requester_id() {
        assert_eq!(
            sql_fragment(Some("requester"), false, false),
            (" AND assigned_to = ?", vec!["requester".to_string()])
        );
    }

    #[test]
    fn a_missing_requester_id_degrades_to_an_unsatisfiable_fragment_not_empty_string_match() {
        assert_eq!(sql_fragment(None, false, false), (" AND 1=0", vec![]));
    }

    #[test]
    fn an_empty_requester_id_also_degrades_to_unsatisfiable() {
        assert_eq!(sql_fragment(Some(""), false, false), (" AND 1=0", vec![]));
    }
}
