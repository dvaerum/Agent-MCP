"""SQLite maintenance helpers.

Per item 11 of the 2026-06-02 database review:

  * `PRAGMA wal_checkpoint(PASSIVE)` periodically truncates the WAL
    file. Without it, under sustained write load the WAL can grow
    unboundedly between auto-checkpoints. PASSIVE means "don't wait
    for readers" — checkpoint as much as possible without blocking.

  * `PRAGMA optimize` updates the query planner's statistics for
    indexed tables. Cheap to call; recommended for periodic execution
    in the SQLite docs (see
    https://sqlite.org/pragma.html#pragma_optimize).

`run_wal_maintenance(conn)` is the synchronous entry point — call it
from a background task scheduled by the lifespan. Returns a result
dict so callers can log structured outcomes; never raises.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger("agent_mcp.db.maintenance")


def run_wal_maintenance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run a PASSIVE wal_checkpoint + PRAGMA optimize on `conn`.

    Returns:
        On success:
          {"checkpoint": (busy:int, log:int, checkpointed:int),
           "optimize_ran": True}
        On failure:
          {"error": str(exc)}

    Doesn't raise — callers expect a structured outcome dict so the
    nightly scheduler stays resilient to transient locking issues.
    """
    out: dict[str, Any] = {}
    try:
        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if row is not None and len(row) >= 3:
            out["checkpoint"] = (int(row[0]), int(row[1]), int(row[2]))
        else:
            out["checkpoint"] = (0, 0, 0)
        # PRAGMA optimize returns no rows; just executing it is the
        # success signal.
        conn.execute("PRAGMA optimize")
        out["optimize_ran"] = True
        return out
    except Exception as exc:  # pragma: no cover — defensive
        msg = f"{type(exc).__name__}: {exc}"
        logger.warning("WAL maintenance failed: %s", msg)
        return {"error": msg}
