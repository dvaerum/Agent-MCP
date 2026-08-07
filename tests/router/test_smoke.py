"""Tiny smoke test: ``agent_mcp.router.app`` + ``…project_registry``
import cleanly and ``make_app()`` returns a non-empty aiohttp app.

If this test goes red, every other test in ``tests/router/`` is going
to fail for the same root cause — start debugging here.
"""

from __future__ import annotations


def test_router_module_imports(router_module) -> None:
    assert hasattr(router_module, "make_app")
    assert hasattr(router_module, "_REGISTRY")
    # The stubbed-out systemctl recorder is in place. The seam lives in
    # ONE module (project_orchestrator); the app reaches it as
    # ``_po._systemctl``, so that's where the stub lands.
    from agent_mcp.router import project_orchestrator as _po

    assert callable(_po._systemctl)


def test_make_app_returns_app_with_routes(router_app) -> None:
    routes = list(router_app.router.routes())
    assert routes, "router app should have at least one route"


def test_project_registry_importable() -> None:
    from agent_mcp.router import project_registry

    assert hasattr(project_registry, "ProjectRegistry")
