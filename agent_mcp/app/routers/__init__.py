"""Per-resource ``APIRouter`` subpackage for the dashboard REST surface.

Wave 8 of prancy-napping-pie splits the 2760-line ``app/routes.py``
into one ``APIRouter`` per resource, mounted on the main ``FastAPI``
app via :func:`register_routers`. The split follows FastAPI's
official "Bigger Applications" pattern and mirrors the per-resource
convention already used under ``agent_mcp/router/`` for the aiohttp
admin surface.

**Scaffold status (PR 0 of 3)**: this module exports the registration
entrypoint and the six per-resource ``APIRouter`` objects, each with
its prefix + router-level ``Depends(require_operator_session)``
already wired. Handler bodies still live in ``app/routes.py`` and
``register_routers`` is **not** called from ``main_app.create_app``
yet — ``register_routes`` continues to own live route registration
through PR 1. PR 2 swaps the call site in ``main_app`` and deletes
the ``routes.py`` shim.

The include order below is alphabetical by router module name; the
order is not load-bearing because each router declares a disjoint
URL prefix, so resolution is deterministic regardless of registration
sequence. Keeping it alphabetical keeps the diff stable when a future
PR adds a seventh router.
"""

from __future__ import annotations

from fastapi import FastAPI

from .agents import router as agents_router
from .composition import router as composition_router
from .memories import router as memories_router
from .messages import router as messages_router
from .settings import router as settings_router
from .tasks import router as tasks_router


def register_routers(app: FastAPI) -> None:
    """Mount each per-resource ``APIRouter`` on ``app``.

    Called from :func:`agent_mcp.app.main_app.create_app` starting in
    PR 2 of Wave 8 (replacing the legacy ``register_routes`` call).
    PR 0 + PR 1 leave the live registration on ``register_routes``;
    this function exists from PR 0 so the import surface is stable
    and tests can verify the subpackage compiles cleanly.
    """
    app.include_router(agents_router)
    app.include_router(composition_router)
    app.include_router(memories_router)
    app.include_router(messages_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)


__all__ = ["register_routers"]
