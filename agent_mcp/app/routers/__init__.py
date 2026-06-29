"""Per-resource ``APIRouter`` subpackage for the dashboard REST surface.

Wave 8 of prancy-napping-pie splits the 2760-line ``app/routes.py``
into one ``APIRouter`` per resource, mounted on the main ``FastAPI``
app via :func:`register_routers`. The split follows FastAPI's
official "Bigger Applications" pattern and mirrors the per-resource
convention already used under ``agent_mcp/router/`` for the aiohttp
admin surface.

**PR 1 status (handlers migrated)**: every dashboard REST handler
has moved from ``app/routes.py`` to its target per-resource router
module here. ``app/routes.py`` is now a thin back-compat shim that
re-exports ``_dispatch_through_tool`` (for tests) and forwards
``register_routes(app)`` to :func:`register_routers`. PR 2 swaps
``main_app.create_app`` to call ``register_routers`` directly and
deletes the shim.

The registration order is **not** purely alphabetical: ``settings``
ships LAST because it owns the ``/api/{path:path}`` OPTIONS catch-all
that mirrors the legacy ``_dashboard_route_specs`` tail. With
first-match-wins routing, registering ``settings`` after every other
``/api``-prefixed router (notably ``tasks``) keeps the per-resource
OPTIONS registrations intact — the catch-all only fires for unknown
``/api/<...>`` paths.
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

    Called from :func:`agent_mcp.app.main_app.create_app` via the
    ``register_routes`` shim in ``app/routes.py`` (PR 1) until PR 2
    of Wave 8 swaps the call site and deletes the shim.

    Order is deliberate: ``settings`` is registered last because it
    owns the catch-all OPTIONS handler at ``/api/{path:path}``.
    With first-match-wins routing the per-resource OPTIONS routes
    (registered earlier) win for their specific paths; the catch-all
    only fires for paths that no concrete route matched.
    """
    app.include_router(agents_router)
    app.include_router(composition_router)
    app.include_router(memories_router)
    app.include_router(messages_router)
    app.include_router(tasks_router)
    # settings registers LAST because it owns the catch-all OPTIONS
    # handler; see module docstring + register_routers docstring.
    app.include_router(settings_router)


__all__ = [
    "agents_router",
    "composition_router",
    "memories_router",
    "messages_router",
    "register_routers",
    "settings_router",
    "tasks_router",
]
