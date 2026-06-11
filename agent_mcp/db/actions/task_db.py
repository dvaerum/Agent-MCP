"""Backwards-compatible re-export shim for the task DB surface.

.. deprecated:: PR 7 (architecture-review series — the "Task flip")
   The implementations of these functions moved to
   :mod:`agent_mcp.repositories.task_repository` so the
   ``TaskRepository`` class is the **single owner** of the task
   cache+DB invariant in fact, not just in the docstring.

   This module remains as a thin re-export so existing importers keep
   working unchanged:

   * ``agent_mcp.cli`` — TUI snapshot reads
   * ``agent_mcp.core.repositories.task_repo`` — the older
     module-of-functions repo
   * Tests that pin the read-side ORM cutover
     (``tests/test_sqlalchemy_task.py``)

   New code should import from the repository (via the
   ``task_repo`` singleton or directly from
   ``agent_mcp.repositories.task_repository``).

Function signatures + return shapes (Optional[Dict[str, Any]] /
List[Dict[str, Any]] keyed by column name, JSON-typed fields
deserialised to Python lists) are preserved 1:1 — re-exports do not
wrap or filter.
"""

from __future__ import annotations

from ...repositories.task_repository import (
    _JSON_LIST_FIELDS,
    _MUTABLE_FIELDS,
    get_all_tasks_from_db,
    get_task_by_id,
    get_tasks_by_agent_id,
    update_task_fields_in_db,
)

__all__ = [
    "get_all_tasks_from_db",
    "get_task_by_id",
    "get_tasks_by_agent_id",
    "update_task_fields_in_db",
    # Underscore-prefixed exports preserved because
    # ``agent_mcp.repositories.task_repository`` (pre-flip) imported them
    # from here. Post-flip the direction is reversed, but external code
    # that happened to import them still works.
    "_JSON_LIST_FIELDS",
    "_MUTABLE_FIELDS",
]
