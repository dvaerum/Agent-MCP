"""Backwards-compatible re-export shim for the agent DB surface.

.. deprecated:: PR 8 (architecture-review series — the "Agent flip")
   The implementations of these functions moved to
   :mod:`agent_mcp.repositories.agent_repository` so the
   ``AgentRepository`` class is the **single owner** of the agent
   dual-cache+DB invariant in fact, not just in the docstring.

   This module remains as a thin re-export so existing importers keep
   working unchanged:

   * ``agent_mcp.cli`` — TUI snapshot reads
   * ``agent_mcp.core.auth`` — token-based auth hot path
   * ``agent_mcp.app.server_lifecycle`` — lifespan hydrate-cache reads
   * ``agent_mcp.core.repositories.agent_repo`` — the older
     module-of-functions repo
   * Tests that pin the read-side ORM cutover
     (``tests/test_sqlalchemy_agent.py``)

   New code should import from the repository (via the
   ``agent_repo`` singleton or directly from
   ``agent_mcp.repositories.agent_repository``).

Function signatures + return shapes (Optional[Dict[str, Any]] /
List[Dict[str, Any]] keyed by column name, ``capabilities``
deserialised to a Python list) are preserved 1:1 — re-exports do not
wrap or filter.
"""

from __future__ import annotations

from ...repositories.agent_repository import (
    _MUTABLE_FIELDS,
    _agent_to_dict,
    get_agent_by_id,
    get_agent_by_token,
    get_all_active_agents_from_db,
    update_agent_db_field,
)

__all__ = [
    "get_agent_by_id",
    "get_agent_by_token",
    "get_all_active_agents_from_db",
    "update_agent_db_field",
    # Underscore-prefixed exports preserved because
    # ``agent_mcp.repositories.agent_repository`` (pre-flip) imported
    # them from here. Post-flip the direction is reversed, but external
    # code that happened to import them still works.
    "_MUTABLE_FIELDS",
    "_agent_to_dict",
]
