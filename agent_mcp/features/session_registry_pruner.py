"""Periodic background task: prune stale rows from `mcp_sessions`.

Streamable HTTP GET /mcp streams self-register on open and
self-unregister on a clean close. A crash, network partition, or
ungraceful client disconnect leaves the row behind — `last_seen_at`
stops advancing but nothing deletes the row.

This task sweeps every `interval_seconds` (default 60s) and calls
`session_registry.expire_stale(threshold_seconds)` (default 300s) to
evict rows whose `last_seen_at` is older than the threshold. The
thresholds are chosen so a healthy stream's heartbeats keep it alive
with plenty of margin, while a dropped stream is reaped before its
absence causes operator confusion ("why does the registry say bob
is subscribed when bob's process died 20 minutes ago?").

Pattern mirrors `features/message_retention.py::run_message_retention_periodically`.
"""

from __future__ import annotations

import asyncio
from typing import NoReturn

from ..core import globals as g
from ..core import session_registry
from ..core.config import logger


DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_THRESHOLD_SECONDS = 300


async def run_session_registry_pruner(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    *,
    task_status=None,
) -> NoReturn:
    """Sweep stale `mcp_sessions` rows on a fixed cadence.

    Sleeps in <=60-second slices so a shutdown / cancel propagates
    promptly rather than blocking for a full interval.
    """
    if task_status is not None:
        task_status.started()

    logger.info(
        "Session registry pruner started (interval=%ds, threshold=%ds)",
        interval_seconds,
        threshold_seconds,
    )

    while g.server_running:
        try:
            # SQLite call inside a thread so we don't stall the event loop.
            expired = await asyncio.to_thread(
                session_registry.expire_stale, threshold_seconds,
            )
            if expired:
                logger.info(
                    "session_registry: expired %d stale session(s): %s",
                    len(expired),
                    ", ".join(expired[:8]) + ("…" if len(expired) > 8 else ""),
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                "Session registry pruner cycle failed: %s", e, exc_info=True,
            )

        remaining = interval_seconds
        while remaining > 0 and g.server_running:
            slice_seconds = min(60, remaining)
            await asyncio.sleep(slice_seconds)
            remaining -= slice_seconds
