# Agent-MCP/agent_mcp/features/message_retention.py
"""Background pruner for the agent_messages table.

The agent_messages table grows unbounded — rows are only ever flipped to
`read=1`, never deleted. This module adds a per-project retention knob
stored in project_context:

  config_message_retention_days:
    absent or 0   -> unbounded retention (no pruning, upstream behavior)
    positive int N -> delete rows where read=1 AND timestamp older than
                      now() - N days

`prune_old_messages()` runs the SQL once. `run_message_retention_periodically()`
is the long-running background task; it sleeps 24h between runs since the
table grows slowly and pruning needs no urgency.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import NoReturn

from ..core.config import logger
from ..core import globals as g
from ..db.connection import get_db_connection


# Default loop interval (24 hours). Settable via env for tests / ops.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60


def _read_retention_days() -> int:
    """Read config_message_retention_days from project_context.

    Returns 0 (disabled) when:
    - the key is absent
    - the value is not a positive integer
    - reading or parsing throws

    Be liberal in what we accept — project_context.value is JSON-encoded
    on write but tests / external tools may push raw strings.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
            ("config_message_retention_days",),
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return 0
    if not row:
        return 0

    raw = row["value"]
    # First try parsing as JSON (the canonical write format).
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        parsed = raw

    try:
        days = int(parsed)
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0


def prune_old_messages() -> int:
    """Delete read messages older than the configured retention window.

    Returns the number of rows deleted. Safe to call when retention is
    disabled (returns 0 without touching the table).
    """
    days = _read_retention_days()
    if days <= 0:
        return 0

    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agent_messages WHERE read = 1 AND timestamp < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted:
            logger.info(
                "Message retention: deleted %d read messages older than %s "
                "(retention=%d days)",
                deleted, cutoff, days,
            )
        return deleted
    except Exception as e:
        logger.error("Message retention pruner failed: %s", e, exc_info=True)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return 0
    finally:
        if conn:
            conn.close()


async def run_message_retention_periodically(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS, *, task_status=None
) -> NoReturn:
    """Background task: run prune_old_messages() every `interval_seconds`.

    Sleeps in small slices so the task can react to shutdown / cancel
    promptly rather than blocking for 24h.
    """
    if task_status is not None:
        task_status.started()

    logger.info(
        "Message retention pruner started (interval=%ds)", interval_seconds
    )

    # Run prune_old_messages in a thread so the sqlite call doesn't
    # block the event loop (same pattern as other DB-touching tasks).
    while g.server_running:
        try:
            await asyncio.to_thread(prune_old_messages)
        except Exception as e:
            logger.error("Message retention cycle failed: %s", e, exc_info=True)

        # Sleep in 60-second slices so we honor server_running quickly.
        remaining = interval_seconds
        while remaining > 0 and g.server_running:
            slice_seconds = min(60, remaining)
            await asyncio.sleep(slice_seconds)
            remaining -= slice_seconds
