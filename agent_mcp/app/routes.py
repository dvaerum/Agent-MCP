# Agent-MCP/mcp_template/mcp_server_src/app/routes.py
"""Back-compat shim for the legacy ``routes.py`` import surface.

Wave 8 PR 1 of prancy-napping-pie mechanically moved every
dashboard REST handler from this file into the per-resource
``APIRouter`` modules under ``agent_mcp/app/routers/``. The module
shrinks from ~2920 lines to this stub, which exists for two
reasons:

  1. ``main_app.create_app`` still calls ``register_routes(app)``
     (the symbol it imported in PR 0). PR 1 keeps that call site
     working by forwarding to :func:`register_routers` from the
     new subpackage. PR 2 swaps the call site to
     ``register_routers`` directly and deletes this file.

  2. A handful of tests import ``_dispatch_through_tool`` and
     ``_dashboard_route_specs`` from ``agent_mcp.app.routes``.
     Re-exporting both keeps those tests passing without test
     edits — which the PR-1 discipline requires (production
     changes only; URL + behavior parity is the gate). The
     ``_dispatch_through_tool`` re-export points at the new home
     in ``agent_mcp.app._dispatch_helpers``; the
     ``_dashboard_route_specs`` re-export is an empty list because
     the spec table is gone — its consumer test
     (``tests/test_wave7_pr3_cleanup.py``) asserts the *absence*
     of specific entries, so an empty list still satisfies it.
"""

from __future__ import annotations

from fastapi import FastAPI

# Re-exported for backwards compatibility with tests that import
# ``_dispatch_through_tool`` from this module. The function moved
# to ``_dispatch_helpers`` in PR 1 so multiple router modules can
# share it without depending on this shim.
from ._dispatch_helpers import _dispatch_through_tool  # noqa: F401
from .routers import (
    agents_router,
    composition_router,
    memories_router,
    messages_router,
    register_routers,
    settings_router,
    tasks_router,
)


def _collect_route_specs() -> list:
    """Reconstruct the legacy ``_dashboard_route_specs`` tuple shape.

    Wave 8 PR 1 retired the literal spec table (route registration
    happens inside each per-resource ``APIRouter`` now). Tests that
    still introspect ``_dashboard_route_specs`` —
    :func:`tests.test_wave7_pr3_cleanup.test_legacy_create_agent_routes_are_not_registered`
    — walk it as a list of ``(path, handler, methods, name)``
    4-tuples to assert presence / absence of specific routes. We
    rebuild the same shape on demand by walking the routers'
    declared routes, so the test contract holds without dragging
    the spec literal back into this shim.
    """
    specs: list = []
    for r in (
        agents_router,
        composition_router,
        memories_router,
        messages_router,
        tasks_router,
        settings_router,
    ):
        # FastAPI's ``APIRouter.routes`` exposes ``Route`` objects
        # whose ``.path`` already includes the router prefix (the
        # decorator splice happens at route-construction time, not
        # at ``include_router`` time). No second prefix prepend.
        for route in r.routes:
            path = getattr(route, "path", "")
            endpoint = getattr(route, "endpoint", None)
            methods = sorted(getattr(route, "methods", set()) or [])
            name = getattr(route, "name", None)
            specs.append((path, endpoint, methods, name))
    return specs


# Module-level snapshot for test imports that don't go through a
# factory. Computed eagerly at module-import time (the routers are
# fully constructed before this module runs because
# ``register_routers`` is imported above, which transitively imports
# every per-resource router and decorates all their routes).
_dashboard_route_specs: list = _collect_route_specs()


def register_routes(app: FastAPI) -> None:
    """Back-compat shim. PR 2 of Wave 8 deletes this file entirely.

    Forwards to :func:`agent_mcp.app.routers.register_routers`,
    which mounts every per-resource ``APIRouter`` on ``app``.
    """
    register_routers(app)


__all__ = [
    "_dispatch_through_tool",
    "_dashboard_route_specs",
    "register_routes",
]
