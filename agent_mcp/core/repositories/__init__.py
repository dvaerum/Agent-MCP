# Agent-MCP/agent_mcp/core/repositories/__init__.py
"""Per-concept repositories that own the cache+DB-as-truth invariant.

Each repo (Task, Agent, Message, Context) provides:

1. **Read interface** — ``get_*`` / ``list_*`` calls that consult the
   in-memory cache from :mod:`agent_mcp.core.state` first, then fall
   through to the database on miss (warm-on-miss populates the cache
   for next time).
2. **Write interface** — ``create_*`` / ``update_*`` calls that write
   the DB FIRST, then update/invalidate the cache, then publish to the
   :class:`EventBus` (if :mod:`agent_mcp.core.event_bus` is available;
   gracefully no-op otherwise).
3. **Test mode** — ``disable_cache()`` context manager that suspends
   the cache for the duration of the ``with`` block so tests can
   exercise DB-only behaviour without dealing with cache state.

PR-W2c introduces these alongside the existing legacy callsites that
still touch ``state.tasks`` / ``state.active_agents`` directly. The
legacy cache stays — repos *maintain* it — until a follow-up PR can
mechanically delete it once no callers remain.

Design notes:

* The repos delegate DB I/O to ``agent_mcp.db.actions.*_db`` for
  tasks / agents / messages (those modules already wrap the
  SQLAlchemy ORM models). :class:`ContextRepository` is ORM-aware
  directly because the actions-layer placeholder is a stub.
* Cache invalidation strategy is "update on write, evict on delete":
  the repo writes the new row, then mutates the cache in-place so
  every caller — including the legacy ones still doing
  ``state.tasks[task_id]`` — sees the new value. Eviction (``del``)
  matches the legacy ``del`` pattern from the tools.
* EventBus is a *soft* dependency. The repos use
  ``importlib.import_module("agent_mcp.core.event_bus")`` inside a
  try/except so that this PR can land before or after the W2b PR
  that creates the bus.
"""
from __future__ import annotations

from . import agent_repo, context_repo, message_repo, task_repo

__all__ = ["agent_repo", "context_repo", "message_repo", "task_repo"]
