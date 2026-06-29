"""Composition reads router — cross-resource dashboard endpoints.

Hosts the handlers that pull from multiple resources (agents +
tasks + memory) to shape one payload for the dashboard UI:
``simple_status``, ``graph_data``, ``task_tree_data``,
``node_details``, ``all_data``. They don't fit cleanly under a
single per-resource prefix, so the router uses the bare
``/api/{project}`` prefix and each handler will register its own
full path (e.g. ``@router.get("/all-data")`` → mounted at
``/api/{project}/all-data``).

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the bare
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the composition
handlers out of ``app/routes.py`` onto this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api/{project}",
    dependencies=[Depends(require_operator_session)],
    tags=["composition"],
)
