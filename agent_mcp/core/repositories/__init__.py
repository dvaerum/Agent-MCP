# Agent-MCP/agent_mcp/core/repositories/__init__.py
"""Residual package for the retired module-of-functions repositories.

Historically this package held the PR #137 "per-concept repositories"
as a *module of functions* (``agent_repo`` / ``task_repo`` /
``message_repo`` / ``context_repo``). The architecture review ruled
those shadowed the canonical class-based repositories under
:mod:`agent_mcp.repositories` — same tables, same
``state.active_agents`` / ``state.agent_working_dirs`` caches — wired
through an *inverted* ``db/actions`` re-export shim. The four modules
were deleted (arch-deepening R3 #2a); every call site now imports the
canonical singleton form::

    from agent_mcp.repositories import agent_repo  # AgentRepository singleton
    agent = agent_repo.get_by_token(bearer_token)

The eager ``from . import agent_repo, …`` that used to live here was
also the head of the circular-import cycle documented in
``agent_mcp/repositories/agent_repository.py`` (the shim → db/actions →
TOP repo loop). Removing it removes the cycle.

Only :mod:`_event_bus_shim` remains under this package — a
soft-dependency EventBus adapter still consumed by
``repositories.agent_repository``, ``repositories.rag_repository``,
``db.unit_of_work``, ``tools.task_tools`` and
``app.routers.composition``. It is imported directly as a submodule
(``from agent_mcp.core.repositories import _event_bus_shim``), so this
``__init__`` deliberately imports nothing. Relocating the shim to a
non-``repositories`` home is left to #2b.
"""
from __future__ import annotations

__all__: list[str] = []
