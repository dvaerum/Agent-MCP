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
from ..repositories import message_repo


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

    PR 9 (Message flip): the DELETE goes through
    ``message_repo.prune_read_before`` so the only DELETE against
    ``agent_messages`` lives in one place. The repo wraps SQLAlchemy
    error handling identically; behaviour on the read-and-old tail
    is unchanged.
    """
    days = _read_retention_days()
    if days <= 0:
        return 0

    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()

    deleted = message_repo.prune_read_before(cutoff)
    if deleted:
        logger.info(
            "Message retention: deleted %d read messages older than %s "
            "(retention=%d days)",
            deleted, cutoff, days,
        )
    return deleted


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

    # Defer the first cycle until lifespan startup finishes — same
    # rationale as session_registry_pruner. This pruner uses raw
    # `get_db_connection()` rather than the ORM, so the failure mode is
    # less severe (a per-cycle reopen, no engine-cache poisoning); we
    # still gate for consistency so every DB-touching bg task has the
    # same startup contract.
    await g.startup_complete_event.wait()

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
