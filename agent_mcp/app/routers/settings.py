"""Settings router — non-CRUD, router-config-shaped endpoints.

Hosts the handlers that surface configuration-shaped reads which
aren't owned by any single resource: ``tokens``, ``aoe_health``,
``prompts_catalog``. Three handlers in three different conceptual
buckets, but all share the "router-config-shaped, non-CRUD" shape
(Wave 8 design decision; see `prancy-napping-pie.md` — open item:
split if any of the three grows).

The router uses the bare ``/api`` prefix (rather than a
per-resource sub-prefix) and each handler will register its own
full path (e.g. ``@router.get("/aoe/health")`` → mounted at
``/api/aoe/health``).

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the bare
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the settings
handlers out of ``app/routes.py`` onto this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_operator_session)],
    tags=["settings"],
)
