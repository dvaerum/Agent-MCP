# Agent-MCP/agent_mcp/db/atomic.py
"""Atomic write-plus-audit context manager.

This module names — in code — an invariant that has lived by convention
across `agent_mcp/tools/`: **every successful write operation produces
exactly one audit row.** Prior to this seam, handler functions spelled
the invariant out longhand:

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE ...")
        log_agent_action_to_db(cursor, agent_id, "operation_name", ...)
        conn.commit()
    finally:
        conn.close()

The pattern is mechanical, but the audit-log call is *separated* from
the connection setup. A refactor that drops `log_agent_action_to_db()`
leaves the write succeeding silently — no audit trail, no error. The
context manager makes the audit-row identity a *required parameter of
the seam itself*: ``operation=`` is keyword-only and mandatory, so
forgetting it becomes a `TypeError` at the call site rather than a
quiet production bug.

Usage:

    from agent_mcp.db.atomic import atomic_with_audit

    with atomic_with_audit(
        operation="task.assign",
        actor="admin",
        task_id=new_task_id,
        details={"agent_id": target_agent_id, "title": task_title},
    ) as cursor:
        cursor.execute("INSERT INTO tasks (...) VALUES (...)", task_data)
        cursor.execute("UPDATE agents SET current_task = ? ...", (...))

Semantics:

* On success — the yielded cursor's writes are committed; one
  `agent_actions` row is written with the supplied `operation` /
  `actor` / `task_id` / `details`; the connection is closed.
* On exception inside the block — the transaction rolls back, *no*
  audit row is written, the exception re-raises. The connection is
  closed in `finally`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .actions.agent_actions_db import log_agent_action_to_db
from .connection import get_db_connection


@contextmanager
def atomic_with_audit(
    *,
    operation: str,
    actor: Optional[str] = None,
    task_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> Iterator[sqlite3.Cursor]:
    """Open a DB transaction paired with exactly one audit-log row.

    The seam yields a `sqlite3.Cursor` for the caller to issue one or
    more write statements. On successful block exit, one row is
    appended to `agent_actions` with the supplied `operation` (as
    `action_type`), `actor` (as `agent_id`), `task_id`, and `details`,
    and the transaction is committed.

    On exception, the transaction is rolled back, no audit row is
    written, and the exception re-raises.

    Args:
        operation: The action_type for the audit row. **Required**
            keyword. Naming this on the seam itself is the whole
            point — a missed audit-log call is now a missed
            mandatory argument, caught at call time rather than
            silently in production.
        actor: The agent_id performing the action (e.g. ``"admin"``
            or a worker agent's id). ``None`` is allowed and stored
            as a NULL `agent_id` for system-level operations that
            have no acting principal.
        task_id: Optional related task id. Stored verbatim on the
            audit row.
        details: Optional dict of extra context. Serialized to JSON
            by `log_agent_action_to_db`. ``None`` is allowed.

    Yields:
        A `sqlite3.Cursor` bound to a fresh connection.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        yield cursor
        # If we get here, the block completed without raising — log
        # the audit row, then commit both the caller's writes and the
        # audit row in a single transaction.
        log_agent_action_to_db(
            cursor,
            agent_id=actor,
            action_type=operation,
            task_id=task_id,
            details=details,
        )
        conn.commit()
    except BaseException:
        # Roll back any pending writes from the caller's block.
        # `log_agent_action_to_db` itself swallows sqlite errors
        # internally, so the only way we reach here is the caller's
        # code raising. The audit row deliberately does NOT get
        # written on the failure path — an event that did not happen
        # should not appear in the audit log.
        try:
            conn.rollback()
        except sqlite3.Error:
            # If rollback itself fails the connection is already
            # poisoned; the close() in finally will tear it down.
            pass
        raise
    finally:
        conn.close()
