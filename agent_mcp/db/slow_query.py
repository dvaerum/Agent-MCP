"""Slow-query logging for the SQLAlchemy engine.

Per item 6 of the 2026-06-02 database review, every query routed
through the SQLAlchemy engine (every ORM table once item 8 lands;
today only `project_context`) is timed via `before_cursor_execute`
and `after_cursor_execute` event listeners. Anything that takes more
than `SLOW_QUERY_THRESHOLD_MS` is surfaced as a WARNING on the
`agent_mcp.db.slow_query` logger so a regression in query plans
becomes immediately visible.

The threshold is intentionally tight (100 ms): every query against
this DB hits a local SQLite file with no network, so anything that
crosses the threshold is either a missing index, an unbounded scan,
or a runaway transaction.

The SQL is truncated to `SQL_PREVIEW_CHARS` to keep log lines
bounded — slow-query investigation needs the query shape and the
duration, not the full payload (which is often interpolated bind
values that swamp the log file).

This module is import-side-effect-free; `install(engine)` is the
entry point. The SQLAlchemy engine constructor in
`agent_mcp/db/engine.py` calls it once per engine.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine


SLOW_QUERY_THRESHOLD_MS = 100
SQL_PREVIEW_CHARS = 200

# Distinct logger so operators can route slow-query records to its own
# sink without raising the level of every db log.
logger = logging.getLogger("agent_mcp.db.slow_query")


# Per-cursor start timestamp. We stash on `context._query_start_time`
# rather than a module-level dict so concurrent queries don't collide
# and the data is GC'd with the context.
_START_ATTR = "_agent_mcp_query_start"


def _before_cursor_execute(
    _conn: Any,
    _cursor: Any,
    _statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    setattr(context, _START_ATTR, time.perf_counter())


def _after_cursor_execute(
    _conn: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    start = getattr(context, _START_ATTR, None)
    if start is None:
        return
    elapsed_s = time.perf_counter() - start
    elapsed_ms = elapsed_s * 1000.0
    if elapsed_ms < SLOW_QUERY_THRESHOLD_MS:
        return
    preview = statement.strip().replace("\n", " ")
    if len(preview) > SQL_PREVIEW_CHARS:
        preview = preview[:SQL_PREVIEW_CHARS] + "…"
    logger.warning(
        "slow query: %.1f ms — %s",
        elapsed_ms,
        preview,
    )


def install(engine: Engine) -> None:
    """Attach the before/after listeners to `engine`.

    Idempotent: if the engine already has our listener, this is a
    no-op. We key idempotency off the listener function identity,
    which SQLAlchemy compares via `contains_event` under the hood.
    """
    if not event.contains(engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    if not event.contains(engine, "after_cursor_execute", _after_cursor_execute):
        event.listen(engine, "after_cursor_execute", _after_cursor_execute)
