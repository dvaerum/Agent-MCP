"""TaskQueryEngine — the business rules for task listing.

This module is the **engine layer** between the MCP handler
(``view_tasks_tool_impl``) and the repository / global task store.
It owns the rules — filter, sort, pagination, dependency analysis,
health metrics — so they're unit-testable without the LLM-text
adapter the handler returns.

Architecture sketch::

    Repository (SQL)
        |
        v
    g.tasks  (Dict[task_id, row])
        |
        v   <-- task_source callable
    TaskQueryEngine.query(filters, sort, offset, limit) -> QueryResult
        |
        v
    view_tasks_tool_impl (handler)  -- presentation: emoji, token
                                       budget, formatting, tips.

The engine ``builds on`` the repository — it does NOT issue SQL. The
caller hands it ``task_source`` (``lambda: g.tasks`` in production, a
dict in tests).

Snapshot semantics: ``query()`` calls ``task_source()`` once and
deep-copies the returned tasks so subsequent mutations to the
underlying store do not race-mutate already-returned results.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.task_ownership import can_access_task
from ..utils.pagination_cache import StableOrderCache


# --- Public dataclasses -----------------------------------------------


@dataclass(frozen=True)
class TaskFilterSpec:
    """Declarative filter rules.

    All fields are optional; ``None`` (or False for boolean flags)
    means "do not filter on this dimension".
    """

    status: Optional[str] = None
    priority: Optional[str] = None
    agent_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    blocked_only: bool = False
    # When ``agent_id`` is set, ``include_unassigned`` widens the match
    # to ``assigned_to == agent_id OR assigned_to IS NULL`` — the
    # worker-facing "my tasks + the claimable pool" visibility rule.
    # Ignored when ``agent_id`` is None (no ownership filter to widen).
    include_unassigned: bool = False
    # Narrow to tasks created by this agent (exact match on ``created_by``).
    created_by: Optional[str] = None
    # Narrow to the unassigned (claimable) pool: ``assigned_to IS NULL``
    # (or empty). A DEDICATED boolean, never a magic ``agent_id`` value,
    # so an agent literally named "unassigned" cannot collide with the
    # sentinel. Composes with the other filters by AND (e.g. a worker's
    # ``agent_id`` + ``unassigned`` narrows their {mine, pool} view to
    # just the pool).
    unassigned: bool = False
    # Complement of ``unassigned``: narrow to tasks that HAVE an assignee
    # (``assigned_to IS NOT NULL``). For a worker (whose visibility is
    # already {mine, pool}) this yields exactly {mine} — the "just my
    # tasks, without the pool" view — and pairs with ``status='incomplete'``
    # for "my open tasks". Setting both ``assigned`` and ``unassigned`` is
    # contradictory and (by AND) matches nothing.
    assigned: bool = False


@dataclass(frozen=True)
class TaskSortSpec:
    """Sort rule.

    Supported keys: ``"created_at"`` (default), ``"updated_at"``,
    ``"priority"``, ``"status"``. All four sort *descending* — that's
    the legacy handler default and the test contract.
    """

    by: str = "created_at"


@dataclass
class TaskHealth:
    """Per-task dependency analysis result."""

    is_blocked: bool = False
    can_start: bool = True
    blocking_dependencies: List[str] = field(default_factory=list)
    completed_dependencies: List[str] = field(default_factory=list)
    missing_dependencies: List[str] = field(default_factory=list)
    blocks_tasks: List[str] = field(default_factory=list)
    dependency_health: str = "healthy"

    def as_dict(self) -> Dict[str, Any]:
        """Render in the legacy ``_analyze_task_dependencies`` shape.

        The handler's ``_format_task_with_dependencies`` reads these
        exact keys, so the engine emits them unchanged for the
        adapter.
        """
        return {
            "is_blocked": self.is_blocked,
            "can_start": self.can_start,
            "blocking_dependencies": list(self.blocking_dependencies),
            "completed_dependencies": list(self.completed_dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "blocks_tasks": list(self.blocks_tasks),
            "dependency_health": self.dependency_health,
        }


@dataclass
class QueryResult:
    """Result of a ``TaskQueryEngine.query()`` call.

    ``tasks`` is the *window* after offset+limit slicing.
    ``total_count`` is the matching-after-filter count BEFORE the
    window — so the caller can render "Total: N" / "showing M of N".
    """

    tasks: List[Dict[str, Any]]
    total_count: int


# --- Constants for the rules (kept module-local on purpose) -----------

_PRIORITY_ORDER = {"high": 3, "medium": 2, "low": 1}
_STATUS_ORDER = {
    "failed": 5,
    "in_progress": 4,
    "pending": 3,
    "completed": 2,
    "cancelled": 1,
}
_REVERSE_SORT_KEYS = {"created_at", "updated_at", "priority", "status"}
_STALE_DAYS = 7
_ACTIVE_STATUSES = {"in_progress", "pending"}

#: Status-filter pseudo-values that expand to "any non-terminal status"
#: (:data:`_ACTIVE_STATUSES` = ``pending`` + ``in_progress``), so a
#: caller can list all open/claimable work in one query instead of
#: asking for each active status separately. Collision-safe: task status
#: is a closed write-time enum (pending/in_progress/completed/cancelled/
#: failed) that never contains these words, so a real task can never HAVE
#: one of these as its status.
INCOMPLETE_STATUS_ALIASES = frozenset({"incomplete", "active", "open"})

#: Terminal task statuses — finished work that is a SINK on both axes:
#: the write side (``_assign_to_existing_tasks`` / the status-transition
#: guard in ``task_tools.py``) refuses to (re)claim these, so the
#: read/discovery "claimable/unassigned pool" MUST exclude them too or it
#: advertises work nobody can claim (R16-F2). This is the single source
#: of truth for that predicate: ``task_tools.py`` imports it (it used to
#: keep a private copy) and the wake seam
#: (``agent_communication_tools._collect_unassigned_task_events_for``)
#: filters its SQL on it. Kept HERE (a leaf feature module) rather than in
#: ``task_tools`` because ``task_tools`` already imports this module — the
#: reverse import would be circular.
TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled", "failed"})


def is_claimable_task(task: Dict[str, Any]) -> bool:
    """The ONE canonical "claimable/unassigned pool" predicate (R16-F2).

    A task is claimable iff it is unassigned (``assigned_to`` is NULL or
    the empty string) AND its status is in the known ACTIVE set
    (:data:`_ACTIVE_STATUSES`). Applied at every read surface (REST
    ``?unassigned=true``, the query engine's ``unassigned`` / worker
    ``include_unassigned`` pool, and the wake seam) so the read side can
    never drift from the write-side terminal sink.

    OBS-R17: this is a positive ALLOWLIST, not a denylist. Testing
    ``status IN active`` rather than ``status NOT IN terminal`` fails
    CLOSED — an unknown / non-enum status (which a future bulk import,
    migration, or new write path could store without going through
    ``update_task``'s enum guard) is treated as NOT claimable, instead of
    slipping the terminal denylist and re-opening the R16-F2 bypass.
    ``_ACTIVE_STATUSES`` is the exact complement of
    :data:`TERMINAL_TASK_STATUSES` over the closed status enum, so the
    two stay in lockstep — no fresh literal to drift.
    """
    if task.get("assigned_to") not in (None, ""):
        return False
    return (task.get("status") or "") in _ACTIVE_STATUSES


def status_filter_matches(want: str, actual: Optional[str]) -> bool:
    """Whether a task whose status is ``actual`` satisfies a ``want``
    status filter. ``want`` is either a concrete status (exact match) or
    one of :data:`INCOMPLETE_STATUS_ALIASES` (matches any active status).
    Shared by ``view_tasks`` (via the engine) and ``search_tasks`` so the
    two surfaces interpret the pseudo-values identically."""
    if want in INCOMPLETE_STATUS_ALIASES:
        return actual in _ACTIVE_STATUSES
    return actual == want


# --- Engine -----------------------------------------------------------


class TaskQueryEngine:
    """Filter / sort / paginate / analyze tasks.

    Parameters
    ----------
    task_source:
        Zero-arg callable returning the current
        ``Dict[task_id, task_row]`` view. In production this is
        ``lambda: g.tasks``; in tests it's the fixture dict.
    now:
        Optional callable returning a ``datetime`` — injected so the
        stale-task rule is deterministic in tests. Defaults to
        ``datetime.datetime.now``.
    pagination_cache:
        Optional :class:`~agent_mcp.utils.pagination_cache.StableOrderCache`
        used to make ``offset``-based pagination survive concurrent
        mutation of the task set (R17-F2 — see that module's
        docstring for the full rationale). Defaults to a private
        instance owned by this engine, so tests get isolation for
        free. Production (``view_tasks_tool_impl``) passes a
        MODULE-LEVEL shared instance explicitly — a fresh
        ``TaskQueryEngine`` is constructed on every tool call, so
        without an explicitly shared cache the anchor would never
        outlive a single call and pagination would be exactly as
        unsafe as before this fix.
    """

    def __init__(
        self,
        task_source: Callable[[], Dict[str, Dict[str, Any]]],
        *,
        now: Optional[Callable[[], _dt.datetime]] = None,
        pagination_cache: Optional[StableOrderCache] = None,
    ) -> None:
        self._task_source = task_source
        self._now = now or _dt.datetime.now
        self._pagination_cache: StableOrderCache = (
            pagination_cache if pagination_cache is not None else StableOrderCache()
        )

    # -- snapshot / dependency helpers ---------------------------------

    def _snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Deep-copy the current task source.

        Deep-copy so callers iterating the result are insulated from
        in-place row mutations by concurrent writers.
        """
        return copy.deepcopy(self._task_source())

    @staticmethod
    def _coerce_deps(value: Any) -> List[str]:
        """Normalize ``depends_on_tasks`` — DB rows store JSON-text."""
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return list(parsed)
            except Exception:
                return []
        return []

    def health_of(
        self,
        task: Dict[str, Any],
        all_tasks: Dict[str, Dict[str, Any]],
    ) -> TaskHealth:
        """Compute the dependency analysis for a single task.

        Mirrors the legacy ``_analyze_task_dependencies`` rules.
        """
        task_id = task.get("task_id")
        status = task.get("status")
        depends_on = self._coerce_deps(task.get("depends_on_tasks", []))

        h = TaskHealth()

        for dep_id in depends_on:
            if dep_id in all_tasks:
                dep_status = all_tasks[dep_id].get("status")
                if dep_status == "completed":
                    h.completed_dependencies.append(dep_id)
                elif dep_status in ("failed", "cancelled"):
                    h.blocking_dependencies.append(dep_id)
                    h.is_blocked = True
                    h.can_start = False
                elif dep_status in ("pending", "in_progress"):
                    h.blocking_dependencies.append(dep_id)
                    if status == "pending":
                        h.can_start = False
            else:
                h.missing_dependencies.append(dep_id)
                h.is_blocked = True
                h.can_start = False

        # Reverse index: which tasks depend on this one
        for other_id, other_task in all_tasks.items():
            other_deps = self._coerce_deps(other_task.get("depends_on_tasks", []))
            if task_id in other_deps:
                h.blocks_tasks.append(other_id)

        # Roll up to a coarse grade for the adapter
        if h.missing_dependencies:
            h.dependency_health = "critical"
        elif h.is_blocked and status == "in_progress":
            h.dependency_health = "warning"
        elif not h.can_start and status == "pending":
            h.dependency_health = "waiting"

        return h

    # -- the rules: filter, sort, paginate -----------------------------

    @staticmethod
    def _matches(
        task: Dict[str, Any],
        filters: TaskFilterSpec,
        all_tasks: Dict[str, Dict[str, Any]],
        blocked_evaluator: Callable[[Dict[str, Any]], bool],
    ) -> bool:
        if filters.status and not status_filter_matches(
            filters.status, task.get("status")
        ):
            return False
        if filters.priority and task.get("priority") != filters.priority:
            return False
        if (
            filters.created_by
            and task.get("created_by") != filters.created_by
        ):
            return False
        if filters.unassigned and not is_claimable_task(task):
            # R16-F2: the "unassigned pool" is the CLAIMABLE pool — a
            # terminal (completed/cancelled/failed) task the write side
            # won't let anyone (re)claim is not part of it.
            return False
        if filters.assigned and task.get("assigned_to") in (None, ""):
            return False
        if filters.agent_id:
            is_own = can_access_task(
                task, requester_id=filters.agent_id, can_view_all_tasks=False
            )
            if filters.include_unassigned:
                # Worker pool visibility: own tasks OR the CLAIMABLE
                # (unassigned + non-terminal) pool. Own tasks stay
                # visible regardless of status (a worker sees its own
                # finished work); the widened pool applies the R16-F2
                # terminal sink so it never advertises dead-end work.
                # Foreign-owned rows still fail the match — cross-worker
                # isolation (AZ-R17-1 / PF-1) holds. NOTE: this is a
                # STRICTER "unassigned" than
                # ``task_ownership.is_unassigned`` (excludes terminal
                # tasks too, R16-F2), so it stays a locally-composed
                # ``is_claimable_task`` check rather than
                # ``can_access_task``'s own ``include_unassigned`` flag.
                if not is_own and not is_claimable_task(task):
                    return False
            elif not is_own:
                return False
        if (
            filters.parent_task_id
            and task.get("parent_task") != filters.parent_task_id
        ):
            return False
        if filters.blocked_only and not blocked_evaluator(task):
            return False
        return True

    @staticmethod
    def _sort_key(task: Dict[str, Any], sort: TaskSortSpec) -> Any:
        if sort.by == "priority":
            return _PRIORITY_ORDER.get(task.get("priority", "medium"), 2)
        if sort.by == "status":
            return _STATUS_ORDER.get(task.get("status", "pending"), 3)
        if sort.by == "updated_at":
            return task.get("updated_at", "")
        # default: created_at
        return task.get("created_at", "")

    def query(
        self,
        *,
        filters: Optional[TaskFilterSpec] = None,
        sort: Optional[TaskSortSpec] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> QueryResult:
        """Run filter -> sort -> paginate against the current snapshot.

        Returns the windowed slice plus ``total_count`` (matching count
        BEFORE the window) so the caller can render pagination hints.

        ``offset``/``limit`` windowing goes through ``pagination_cache``
        (R17-F2): an ``offset == 0`` call always re-filters/re-sorts
        fresh and anchors the resulting id ordering; a following
        ``offset > 0`` call for the SAME ``(filters, sort)`` shape
        replays that anchored ordering (looking up each id's CURRENT
        row data) instead of re-filtering from scratch — so a task
        that leaves the matched set between the two calls can no
        longer shift a still-matching task out of both pages. See
        ``agent_mcp/utils/pagination_cache.py`` for the full rationale
        and the disclosed trade-off of this fix.
        """
        filters = filters or TaskFilterSpec()
        sort = sort or TaskSortSpec()

        snapshot = self._snapshot()

        def is_blocked(task: Dict[str, Any]) -> bool:
            h = self.health_of(task, snapshot)
            return h.is_blocked or not h.can_start

        def compute_ordered_ids() -> List[str]:
            matched = [
                row
                for row in snapshot.values()
                if self._matches(row, filters, snapshot, is_blocked)
            ]
            matched.sort(
                key=lambda t: self._sort_key(t, sort),
                reverse=sort.by in _REVERSE_SORT_KEYS,
            )
            return [row["task_id"] for row in matched]

        ordered_ids = self._pagination_cache.get_or_anchor(
            (filters, sort), offset=offset, compute=compute_ordered_ids
        )

        # R21-F3: ``ordered_ids`` is the anchor frozen at sweep-start —
        # some of those ids may have been deleted outright since. Run
        # the SAME ``tid in snapshot`` liveness check once, up front,
        # and derive both ``total_count`` (its length) and the window
        # (sliced from it below) from that one filtered list — reusing
        # the identical anchor length would over-count a since-deleted
        # row, and computing the window's liveness check independently
        # from the total's is exactly the twin-computation drift this
        # cache class was already bitten by once (R18-F2).
        live_ids = [tid for tid in ordered_ids if tid in snapshot]
        total_count = len(live_ids)
        window_ids = ordered_ids[offset:] if offset else ordered_ids
        if limit is not None:
            window_ids = window_ids[:limit]

        # Rows anchored earlier may since have been deleted outright
        # (as opposed to merely no longer matching ``filters``) — omit
        # them from the window rather than shifting a neighbour into
        # their place (that would just reintroduce the original bug).
        tasks = [snapshot[tid] for tid in window_ids if tid in snapshot]

        return QueryResult(tasks=tasks, total_count=total_count)

    # -- aggregate metrics --------------------------------------------

    def health_metrics(
        self,
        tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Aggregate metrics over a task set.

        When ``tasks`` is None, runs over the current snapshot (used by
        the engine-level health dashboard). When ``tasks`` is provided,
        runs over that explicit list (used by the handler to report
        metrics on the filtered window — matches legacy behavior).
        """
        if tasks is None:
            tasks = list(self._snapshot().values())

        if not tasks:
            return {"total": 0, "status": "no_data"}

        total = len(tasks)
        status_counts: Dict[str, int] = {}
        priority_counts: Dict[str, int] = {}
        blocked_count = 0
        stale_count = 0

        current_time = self._now()

        for task in tasks:
            status = task.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

            priority = task.get("priority", "medium")
            priority_counts[priority] = priority_counts.get(priority, 0) + 1

            deps = self._coerce_deps(task.get("depends_on_tasks", []))
            if deps and status == "pending":
                blocked_count += 1

            updated_at = task.get("updated_at")
            if updated_at:
                try:
                    updated_time = _dt.datetime.fromisoformat(
                        updated_at.replace("Z", "+00:00").replace("+00:00", "")
                    )
                    days_since_update = (current_time - updated_time).days
                    if (
                        days_since_update > _STALE_DAYS
                        and status in _ACTIVE_STATUSES
                    ):
                        stale_count += 1
                except Exception:
                    pass

        completed_ratio = status_counts.get("completed", 0) / total
        active_ratio = (
            status_counts.get("in_progress", 0)
            + status_counts.get("pending", 0)
        ) / total
        blocked_ratio = blocked_count / total if total > 0 else 0
        stale_ratio = stale_count / total if total > 0 else 0

        health_score = max(
            0,
            min(
                100,
                completed_ratio * 30
                + active_ratio * 40
                + (1 - blocked_ratio) * 20
                + (1 - stale_ratio) * 10,
            ),
        )

        if health_score >= 80:
            health_status = "excellent"
        elif health_score >= 60:
            health_status = "good"
        elif health_score >= 40:
            health_status = "needs_attention"
        else:
            health_status = "critical"

        return {
            "total": total,
            "status_distribution": status_counts,
            "priority_distribution": priority_counts,
            "blocked_tasks": blocked_count,
            "stale_tasks": stale_count,
            "health_score": round(health_score, 1),
            "health_status": health_status,
        }


__all__ = [
    "QueryResult",
    "TaskFilterSpec",
    "TaskHealth",
    "TaskQueryEngine",
    "TaskSortSpec",
]
