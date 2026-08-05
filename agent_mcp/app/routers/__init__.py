"""Per-resource ``APIRouter`` subpackage for the dashboard REST surface.

Wave 8 of prancy-napping-pie split the 2760-line ``app/routes.py``
into one ``APIRouter`` per resource, mounted on the main ``FastAPI``
app via :func:`register_routers`. The split follows FastAPI's
official "Bigger Applications" pattern and mirrors the per-resource
convention already used under ``agent_mcp/router/`` for the aiohttp
admin surface.

**Wave 8 status (PR 2 complete)**: every dashboard REST handler now
lives in its target per-resource router module here.
:func:`agent_mcp.app.main_app.create_app` calls :func:`register_routers`
directly; the PR-1 back-compat shim at ``app/routes.py`` is deleted.

The registration order is **not** purely alphabetical: ``settings``
ships LAST because it owns the ``/api/{path:path}`` OPTIONS catch-all
that mirrors the legacy ``_dashboard_route_specs`` tail. With
first-match-wins routing, registering ``settings`` after every other
``/api``-prefixed router (notably ``tasks``) keeps the per-resource
OPTIONS registrations intact — the catch-all only fires for unknown
``/api/<...>`` paths.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from fastapi import APIRouter, FastAPI

from .agents import router as agents_router
from .composition import router as composition_router
from .delivery import router as delivery_router
from .events import router as events_router
from .memories import router as memories_router
from .messages import router as messages_router
from .schedules import router as schedules_router
from .settings import router as settings_router
from .tasks import router as tasks_router


# Per-resource routers in the same order :func:`register_routers`
# mounts them on the app. Exposed as a module-level constant so
# introspection helpers (and tests that walk the wired surface) have
# a single source of truth for "every router this package owns".
_ALL_ROUTERS: Tuple[APIRouter, ...] = (
    agents_router,
    composition_router,
    delivery_router,
    events_router,
    memories_router,
    messages_router,
    schedules_router,
    tasks_router,
    # settings registers LAST because it owns the catch-all OPTIONS
    # handler; see module docstring + register_routers docstring.
    settings_router,
)


def register_routers(app: FastAPI) -> None:
    """Mount each per-resource ``APIRouter`` on ``app``.

    Called from :func:`agent_mcp.app.main_app.create_app` (Wave 8 PR 2
    swapped the call site from the deleted ``register_routes`` shim to
    this function directly).

    Order is deliberate: ``settings`` is registered last because it
    owns the catch-all OPTIONS handler at ``/api/{path:path}``.
    With first-match-wins routing the per-resource OPTIONS routes
    (registered earlier) win for their specific paths; the catch-all
    only fires for paths that no concrete route matched.
    """
    for router in _ALL_ROUTERS:
        app.include_router(router)


def iter_route_specs() -> List[Tuple[str, Any, List[str], Any]]:
    """Walk every per-resource router and yield route 4-tuples.

    Returns a list of ``(path, endpoint, methods, name)`` tuples — the
    same shape the retired ``_dashboard_route_specs`` literal exposed
    in ``app/routes.py``. Tests that introspect the registered route
    surface (presence/absence of a path + method combination) consume
    this helper as a stable, post-shim entry point.

    ``APIRouter.routes`` exposes ``Route`` objects whose ``.path``
    already includes the router prefix (the decorator splice happens
    at route-construction time, not at ``include_router`` time), so
    callers see the same fully-qualified paths the app does.
    """
    specs: List[Tuple[str, Any, List[str], Any]] = []
    for r in _ALL_ROUTERS:
        for route in r.routes:
            path = getattr(route, "path", "")
            endpoint = getattr(route, "endpoint", None)
            methods = sorted(getattr(route, "methods", set()) or [])
            name = getattr(route, "name", None)
            specs.append((path, endpoint, methods, name))
    return specs


__all__ = [
    "agents_router",
    "composition_router",
    "events_router",
    "iter_route_specs",
    "memories_router",
    "messages_router",
    "register_routers",
    "settings_router",
    "tasks_router",
]
