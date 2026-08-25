# Agent-MCP/agent_mcp/repositories/scheduled_directive_repository.py
"""ScheduledDirectiveRepository — cursor-based CRUD + firing for the
``scheduled_directive`` store (event-loop scheduled directives).

A **directive** is an agent's recurring imperative that fires *when the
agent next checks in* at-or-after its interval. This store is pure state
(``next_due_at``); firing is wait-loop-native by default via the
``wait_for_events``/``fetch_events_since`` slice loop, which is always
available and needs no extra infrastructure. Since ADR-0026, a second,
additive path also fires due directives: the delivery-transport
background tick (:mod:`agent_mcp.features.delivery_scheduler`) evaluates
and fires due directives for any worker with a live delivery stream, so a
delivery-connected worker that never calls ``wait_for_events`` still gets
nudged. Both callers share :func:`collect_due_and_fire` unmodified.

Shaped like ``project_settings_repository``: a **module of plain
functions**, each taking a live ``connection`` (a ``sqlite3.Cursor``,
row_factory=``sqlite3.Row``). Callers own the transaction — CRUD tools
supply ``unit_of_work().cursor``; the wait-loop firing/read helpers open
a short-lived connection and commit around :func:`collect_due_and_fire`.

Key invariants encoded here:

* **interval-reset-from-delivery** (decision 3): a fire sets
  ``next_due_at = <delivery-time> + interval`` — never a fixed wall-clock
  grid — so a busy agent never piles up fires.
* **offline fire → once on reconnect** (decision 12): an overdue schedule
  fires exactly once when the agent reconnects; ``next_due_at`` resets
  from that single delivery, not once per missed slot.
* **end-conditions → terminal** (decision 10): when ``run_count`` reaches
  ``max_runs`` or wall-clock passes ``until_at``, the row is marked
  ``status='completed', enabled=0`` and KEPT (listable), not deleted.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional


# The delivered ``directive`` event shape, source="schedule". Consumed by
# both firing paths: the wait_for_events-native collector and, since
# ADR-0026, the delivery-transport background tick.
def _directive_event(
    *, directive_id: str, prompt: str, timestamp: str
) -> Dict[str, Any]:
    return {
        "type": "directive",
        "ref_id": directive_id,
        "timestamp": timestamp,
        "priority": "urgent",
        "data": {
            "prompt": prompt,
            "source": "schedule",
            "schedule_id": directive_id,
        },
    }


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "directive_id": row["directive_id"],
        "agent_id": row["agent_id"],
        "prompt": row["prompt"],
        "interval_seconds": row["interval_seconds"],
        "next_due_at": row["next_due_at"],
        "enabled": int(row["enabled"]),
        "status": row["status"],
        "until_at": row["until_at"],
        "max_runs": row["max_runs"],
        "run_count": int(row["run_count"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


_SELECT_COLS = (
    "directive_id, agent_id, prompt, interval_seconds, next_due_at, "
    "enabled, status, until_at, max_runs, run_count, created_at, "
    "created_by, updated_at, updated_by"
)


def get(directive_id: str, *, connection: Any) -> Optional[Dict[str, Any]]:
    """Fetch one directive by id, or ``None``."""
    connection.execute(
        f"SELECT {_SELECT_COLS} FROM scheduled_directive "
        "WHERE directive_id = ?",
        (directive_id,),
    )
    row = connection.fetchone()
    return _row_to_dict(row) if row is not None else None


def list_for_agent(
    agent_id: str, *, connection: Any
) -> List[Dict[str, Any]]:
    """Every directive for ``agent_id`` (all statuses), soonest-due first."""
    connection.execute(
        f"SELECT {_SELECT_COLS} FROM scheduled_directive "
        "WHERE agent_id = ? ORDER BY next_due_at ASC",
        (agent_id,),
    )
    return [_row_to_dict(r) for r in connection.fetchall()]


def list_all(*, connection: Any) -> List[Dict[str, Any]]:
    """Every directive across the project (dashboard read), soonest-due
    first, grouped by agent."""
    connection.execute(
        f"SELECT {_SELECT_COLS} FROM scheduled_directive "
        "ORDER BY agent_id ASC, next_due_at ASC"
    )
    return [_row_to_dict(r) for r in connection.fetchall()]


def count_active_for_agent(agent_id: str, *, connection: Any) -> int:
    """Number of ENABLED + ``status='active'`` directives for the agent.

    This is the count the ``config_max_schedules_per_agent`` guardrail
    is enforced against — completed/paused schedules do not count.
    """
    connection.execute(
        "SELECT COUNT(*) AS n FROM scheduled_directive "
        "WHERE agent_id = ? AND enabled = 1 AND status = 'active'",
        (agent_id,),
    )
    row = connection.fetchone()
    return int(row["n"]) if row is not None else 0


def create(
    *,
    directive_id: str,
    agent_id: str,
    prompt: str,
    interval_seconds: int,
    next_due_at: str,
    until_at: Optional[str],
    max_runs: Optional[int],
    created_by: Optional[str],
    connection: Any,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """INSERT a fresh active/enabled directive. Returns the row dict."""
    now = now_iso or datetime.datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO scheduled_directive (
            directive_id, agent_id, prompt, interval_seconds, next_due_at,
            enabled, status, until_at, max_runs, run_count,
            created_at, created_by, updated_at, updated_by
        ) VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            directive_id, agent_id, prompt, interval_seconds, next_due_at,
            until_at, max_runs, now, created_by, now, created_by,
        ),
    )
    return {
        "directive_id": directive_id,
        "agent_id": agent_id,
        "prompt": prompt,
        "interval_seconds": interval_seconds,
        "next_due_at": next_due_at,
        "enabled": 1,
        "status": "active",
        "until_at": until_at,
        "max_runs": max_runs,
        "run_count": 0,
        "created_at": now,
        "created_by": created_by,
        "updated_at": now,
        "updated_by": created_by,
    }


def update_fields(
    directive_id: str,
    fields: Dict[str, Any],
    *,
    updated_by: Optional[str],
    connection: Any,
    now_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Partial UPDATE of an existing directive.

    ``fields`` may carry any of: ``prompt``, ``interval_seconds``,
    ``next_due_at``, ``enabled``, ``status``, ``until_at``, ``max_runs``,
    ``run_count``. Unlisted columns are preserved. ``updated_at`` /
    ``updated_by`` always refresh. Returns the updated row dict, or
    ``None`` when the id doesn't exist.
    """
    existing = get(directive_id, connection=connection)
    if existing is None:
        return None
    allowed = {
        "prompt",
        "interval_seconds",
        "next_due_at",
        "enabled",
        "status",
        "until_at",
        "max_runs",
        "run_count",
    }
    now = now_iso or datetime.datetime.now().isoformat()
    set_cols: List[str] = []
    params: List[Any] = []
    for col in allowed:
        if col in fields:
            set_cols.append(f"{col} = ?")
            params.append(fields[col])
    set_cols.append("updated_at = ?")
    params.append(now)
    set_cols.append("updated_by = ?")
    params.append(updated_by)
    params.append(directive_id)
    connection.execute(
        f"UPDATE scheduled_directive SET {', '.join(set_cols)} "
        "WHERE directive_id = ?",
        params,
    )
    return get(directive_id, connection=connection)


def delete(directive_id: str, *, connection: Any) -> bool:
    """DELETE a directive. Returns True iff a row was removed."""
    if get(directive_id, connection=connection) is None:
        return False
    connection.execute(
        "DELETE FROM scheduled_directive WHERE directive_id = ?",
        (directive_id,),
    )
    return True


def soonest_due_at(
    agent_id: str, now_iso: str, *, connection: Any
) -> Optional[str]:
    """Soonest ``next_due_at`` over the agent's still-fireable directives.

    "Fireable" = ``enabled=1 AND status='active'`` AND the ``until_at``
    window has not already closed (``until_at IS NULL OR until_at > now``).
    Returns ``None`` when the agent has no fireable schedule — the wait
    loop treats that as "no scheduled wake condition / idle-stop not
    suppressed".
    """
    connection.execute(
        """
        SELECT MIN(next_due_at) AS soonest FROM scheduled_directive
        WHERE agent_id = ? AND enabled = 1 AND status = 'active'
          AND (until_at IS NULL OR until_at > ?)
        """,
        (agent_id, now_iso),
    )
    row = connection.fetchone()
    return row["soonest"] if row is not None else None


def has_active(agent_id: str, now_iso: str, *, connection: Any) -> bool:
    """True iff the agent has at least one still-fireable directive.

    Same predicate as :func:`soonest_due_at`; this is the idle-stop
    suppression gate (decision 9: an enabled schedule keeps the agent
    present so it can receive fires).
    """
    return soonest_due_at(agent_id, now_iso, connection=connection) is not None


def _compute_next_and_terminal(
    row: Dict[str, Any], now_iso: str
) -> tuple[str, int, bool]:
    """Return ``(new_next_due_at, new_run_count, completed)`` for a fire
    landing at ``now_iso`` (interval-reset-from-delivery)."""
    run_count = int(row["run_count"]) + 1
    now_dt = datetime.datetime.fromisoformat(now_iso)
    new_next_due = (
        now_dt + datetime.timedelta(seconds=int(row["interval_seconds"]))
    ).isoformat()
    completed = False
    if row["max_runs"] is not None and run_count >= int(row["max_runs"]):
        completed = True
    if row["until_at"] is not None and new_next_due > row["until_at"]:
        # The next fire would land beyond the window → this was the last.
        completed = True
    return new_next_due, run_count, completed


def collect_due_and_fire(
    agent_id: str, now_iso: str, *, connection: Any
) -> List[Dict[str, Any]]:
    """Fire every due directive for ``agent_id`` and return the events.

    This is the firing step shared by both callers: the wait_for_events-
    native collector (``_collect_scheduled_directive_events_for``) and, since
    ADR-0026, the delivery-transport background tick
    (``delivery_scheduler._fire_due_directives``). For each enabled, active
    directive that is due (``next_due_at <= now``) OR whose window has
    closed (``until_at <= now``):

    * a still-in-window due schedule emits a ``directive`` event, bumps
      ``run_count``, and resets ``next_due_at = now + interval``
      (interval-reset-from-delivery);
    * a schedule whose ``until_at`` has passed is REAPED — marked
      ``completed``/``enabled=0`` with NO event (it is past its window);
    * end-conditions (``run_count >= max_runs`` or a next-due beyond
      ``until_at``) flip the just-fired schedule to ``completed``.

    The caller owns the transaction/commit. Overdue schedules fire exactly
    ONCE (next_due resets from this single delivery), satisfying the
    offline-reconnect-once contract (decision 12).
    """
    connection.execute(
        f"SELECT {_SELECT_COLS} FROM scheduled_directive "
        "WHERE agent_id = ? AND enabled = 1 AND status = 'active' "
        "AND (next_due_at <= ? OR (until_at IS NOT NULL AND until_at <= ?)) "
        "ORDER BY next_due_at ASC",
        (agent_id, now_iso, now_iso),
    )
    rows = [_row_to_dict(r) for r in connection.fetchall()]
    events: List[Dict[str, Any]] = []
    for row in rows:
        window_closed = (
            row["until_at"] is not None and row["until_at"] <= now_iso
        )
        if window_closed:
            # Past its window — reap without firing.
            connection.execute(
                "UPDATE scheduled_directive "
                "SET status = 'completed', enabled = 0, updated_at = ?, "
                "updated_by = 'system' WHERE directive_id = ?",
                (now_iso, row["directive_id"]),
            )
            continue
        new_next_due, run_count, completed = _compute_next_and_terminal(
            row, now_iso
        )
        if completed:
            connection.execute(
                "UPDATE scheduled_directive "
                "SET run_count = ?, next_due_at = ?, status = 'completed', "
                "enabled = 0, updated_at = ?, updated_by = 'system' "
                "WHERE directive_id = ?",
                (run_count, new_next_due, now_iso, row["directive_id"]),
            )
        else:
            connection.execute(
                "UPDATE scheduled_directive "
                "SET run_count = ?, next_due_at = ?, updated_at = ?, "
                "updated_by = 'system' WHERE directive_id = ?",
                (run_count, new_next_due, now_iso, row["directive_id"]),
            )
        events.append(
            _directive_event(
                directive_id=row["directive_id"],
                prompt=row["prompt"],
                timestamp=now_iso,
            )
        )
    return events


__all__ = [
    "get",
    "list_for_agent",
    "list_all",
    "count_active_for_agent",
    "create",
    "update_fields",
    "delete",
    "soonest_due_at",
    "has_active",
    "collect_due_and_fire",
]
