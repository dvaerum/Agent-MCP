# Agent-MCP/agent_mcp/repositories/pending_directive_repository.py
"""PendingDirectiveRepository — the one-shot **poke** queue.

An operator/admin poke pushes a single directive to an agent out-of-band
(plan §2 decision 11). Delivered immediately if the agent is listening
(the REST route fires a waiter-wake), else queued here and collected on
the agent's next ``wait_for_events`` / ``fetch_events_since`` check-in.

Plain-function module (like ``scheduled_directive_repository``); every
function takes a live ``connection`` cursor. The delivered ``directive``
event carries ``source="poke"`` and ``schedule_id=None`` (plan §3), and
its ``priority`` sorts it to the FRONT of the returned batch.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional


def _poke_event(
    *, poke_id: str, prompt: str, priority: str, timestamp: str
) -> Dict[str, Any]:
    return {
        "type": "directive",
        "ref_id": poke_id,
        "timestamp": timestamp,
        "priority": priority or "urgent",
        "data": {
            "prompt": prompt,
            "source": "poke",
            "schedule_id": None,
        },
    }


def create_poke(
    *,
    poke_id: str,
    agent_id: str,
    prompt: str,
    priority: str = "urgent",
    created_by: Optional[str],
    connection: Any,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """INSERT an undelivered poke row. Returns the row dict."""
    now = now_iso or datetime.datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO pending_directive (
            poke_id, agent_id, prompt, priority, created_at, created_by,
            delivered_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (poke_id, agent_id, prompt, priority, now, created_by),
    )
    return {
        "poke_id": poke_id,
        "agent_id": agent_id,
        "prompt": prompt,
        "priority": priority,
        "created_at": now,
        "created_by": created_by,
        "delivered_at": None,
    }


def collect_undelivered(
    agent_id: str, *, connection: Any, now_iso: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Collect + mark-delivered every undelivered poke for ``agent_id``.

    Returns the ``directive`` events (priority-first order preserved by the
    caller's sort). Mutates ``delivered_at`` so each poke fires exactly
    once. The caller owns the transaction/commit.
    """
    now = now_iso or datetime.datetime.now().isoformat()
    connection.execute(
        "SELECT poke_id, prompt, priority FROM pending_directive "
        "WHERE agent_id = ? AND delivered_at IS NULL "
        "ORDER BY created_at ASC",
        (agent_id,),
    )
    rows = [dict(r) for r in connection.fetchall()]
    events: List[Dict[str, Any]] = []
    for row in rows:
        connection.execute(
            "UPDATE pending_directive SET delivered_at = ? WHERE poke_id = ?",
            (now, row["poke_id"]),
        )
        events.append(
            _poke_event(
                poke_id=row["poke_id"],
                prompt=row["prompt"],
                priority=row["priority"],
                timestamp=now,
            )
        )
    return events


def count_undelivered(agent_id: str, *, connection: Any) -> int:
    connection.execute(
        "SELECT COUNT(*) AS n FROM pending_directive "
        "WHERE agent_id = ? AND delivered_at IS NULL",
        (agent_id,),
    )
    row = connection.fetchone()
    return int(row["n"]) if row is not None else 0


__all__ = [
    "create_poke",
    "collect_undelivered",
    "count_undelivered",
]
