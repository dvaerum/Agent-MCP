"""OBS-R17-CLAIM — ``is_claimable_task`` is a positive ALLOWLIST.

Round-17 observability/hardening. ``is_claimable_task`` (the single
canonical "claimable/unassigned pool" predicate, R16-F2) used to test
``status NOT IN {terminal set}`` — a *denylist*. That was safe only
because ``update_task`` validates status against the closed enum, so a
non-canonical status could not be stored. The moment any future write
path (bulk import, migration, a new tool) writes a raw non-enum status,
that row would slip the denylist and re-open the R16-F2 terminal-exclusion
bypass — a dead-end row re-appearing in the claimable pool.

DECISION (operator, 2026-08-10): flip it to a positive allowlist — a task
is claimable iff it is unassigned AND its status is in the KNOWN ACTIVE
set (the complement of the terminal set the module already defines). An
unknown / garbage status is fail-closed: NOT claimable.

The first test (``test_unknown_status_is_not_claimable``) is the RED: the
old denylist wrongly reported an unknown-status row as claimable.
"""

from __future__ import annotations

from agent_mcp.features.task_queries import (
    TERMINAL_TASK_STATUSES,
    TaskFilterSpec,
    TaskQueryEngine,
    is_claimable_task,
)


def _row(tid: str, status: str, assignee=None) -> dict:
    return {
        "task_id": tid,
        "title": tid,
        "status": status,
        "priority": "medium",
        "assigned_to": assignee,
        "created_by": "admin",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
        "parent_task": None,
        "child_tasks": [],
        "depends_on_tasks": [],
        "notes": [],
    }


# --- the predicate directly --------------------------------------------


def test_unknown_status_is_not_claimable() -> None:
    """RED: an unassigned row with a NON-ENUM status is NOT claimable.

    The old denylist (``status NOT IN terminal``) wrongly included it,
    re-opening the R16-F2 bypass. The allowlist fails it closed.
    """
    assert is_claimable_task(_row("garbage", "totally_bogus")) is False
    # A few more shapes of "not a known active status".
    for junk in ("", "unknown", "archived", "paused", "COMPLETED", "Pending"):
        assert is_claimable_task(_row("j", junk)) is False, junk


def test_active_unassigned_task_is_claimable() -> None:
    """A genuinely active, unassigned task IS claimable."""
    assert is_claimable_task(_row("p", "pending")) is True
    assert is_claimable_task(_row("ip", "in_progress")) is True


def test_each_terminal_status_is_not_claimable() -> None:
    """Every terminal status is excluded (unassigned or not)."""
    for term in TERMINAL_TASK_STATUSES:
        assert is_claimable_task(_row("t", term)) is False, term
        assert is_claimable_task(_row("t", term, "alice")) is False, term
    # explicit spellings, in case the frozenset ever narrows
    for term in ("completed", "cancelled", "failed"):
        assert is_claimable_task(_row("t", term)) is False, term


def test_assigned_active_task_is_not_claimable() -> None:
    """An active task that HAS an assignee is not in the claimable pool."""
    assert is_claimable_task(_row("a", "pending", "alice")) is False
    assert is_claimable_task(_row("a", "in_progress", "bob")) is False
    # empty-string assignee is treated as unassigned (existing contract)
    assert is_claimable_task(_row("a", "pending", "")) is True


# --- R16-F2 read surfaces still behave (regression) --------------------


def test_engine_unassigned_pool_drops_unknown_status() -> None:
    """The query engine ``unassigned`` pool applies the allowlist — an
    unknown-status unassigned row must NOT surface as claimable work."""
    snap = {
        "open": _row("open", "pending"),
        "junk": _row("junk", "weird_status"),
        "done": _row("done", "completed"),
        "assigned": _row("assigned", "pending", "alice"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(filters=TaskFilterSpec(unassigned=True))
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"open"}, ids


def test_engine_worker_pool_drops_unknown_status_but_keeps_own() -> None:
    """The worker self-claim pool (``include_unassigned``) keeps the
    worker's OWN rows regardless of status, but the widened claimable pool
    excludes unknown-status rows (fail-closed)."""
    snap = {
        "pool_open": _row("pool_open", "pending"),
        "pool_junk": _row("pool_junk", "weird_status"),
        "mine_open": _row("mine_open", "in_progress", "alice"),
        "mine_done": _row("mine_done", "completed", "alice"),
        "foreign": _row("foreign", "pending", "bob"),
    }
    engine = TaskQueryEngine(task_source=lambda: snap)
    result = engine.query(
        filters=TaskFilterSpec(agent_id="alice", include_unassigned=True)
    )
    ids = {t["task_id"] for t in result.tasks}
    assert ids == {"pool_open", "mine_open", "mine_done"}, ids
