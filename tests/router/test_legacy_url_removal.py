"""Removal of legacy URL surfaces — formerly 308-redirected for the
30-day grace window after the URL redesign (PR-A through PR-D landed
in v3.x → v4.2.0). The grace window has expired; the redirects are
deleted and these URLs now 404.

Scope (locked by Dennis, ship-the-legacy-cleanup PR, v5.0.0):

  Removed from agent_mcp/router/app.py
    - /agent-mcp/__dashboard/...     → /agent-mcp/app/...           (308 deleted)
    - /agent-mcp/__api/<name>/<rest> → /agent-mcp/api/<name>/<rest> (308 deleted)
    - /agent-mcp/<name>/mcp          → /agent-mcp/mcp/<name>        (308 deleted)

  Removed from agent_mcp/app/routes.py
    - /api/agents-list  (legacy alias for /api/agents)
    - /api/tasks-all    (legacy alias for /api/tasks)

ADR 0014 (v5.0.60) extended this set: the remaining ``__``-prefixed
router endpoints (``__projects``, ``__overview``, ``__create``,
``__rename``, ``__unregister``, ``__alias-usage``, ``__remove-alias``,
``__client-config``, ``__client-installer``, ``__create-agent``)
were retired in favour of REST shapes under ``/api/router/...``. The
404-on-legacy guards for those URLs live in
``test_router_admin_api.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── /agent-mcp/__dashboard/... → 404 (redirect deleted) ──────────────


async def test_legacy_dashboard_bare_returns_404(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("index.html", "<html>x</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/", allow_redirects=False,
    )

    assert resp.status == 404


async def test_legacy_dashboard_project_returns_404(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("index.html", "<html>x</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/proj/", allow_redirects=False,
    )

    assert resp.status == 404


async def test_legacy_dashboard_project_subpath_returns_404(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("index.html", "<html>x</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/proj/tasks/", allow_redirects=False,
    )

    assert resp.status == 404


async def test_legacy_dashboard_next_assets_returns_404(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("_next/static/chunks/main.js", "x")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/_next/static/chunks/main.js",
        allow_redirects=False,
    )

    assert resp.status == 404


# ── /agent-mcp/__api/<name>/<rest> → 404 (redirect deleted) ──────────


async def test_legacy_api_path_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/tokens",
        allow_redirects=False,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


async def test_legacy_api_path_with_query_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("proj")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__api/proj/messages?filter=unread",
        allow_redirects=False,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


# ── /agent-mcp/<name>/mcp → 404 (redirect deleted) ───────────────────


async def test_legacy_mcp_path_returns_404(
    aiohttp_client, router_app, router_module,
) -> None:
    router_module._REGISTRY.register("proj", "/tmp/ws-legacy-mcp-test")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/proj/mcp",
        data=b'{}',
        allow_redirects=False,
    )

    assert resp.status == 404


# ── New URLs still work (regression-guard) ──────────────────────────


async def test_new_app_path_still_serves(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Make sure the deletion of the legacy redirects didn't clip the
    NEW URLs they used to point at."""
    write_dashboard_file("index.html", "<html>dashboard</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/proj/")

    assert resp.status == 200
    assert "dashboard" in await resp.text()


async def test_new_mcp_path_still_routes(
    aiohttp_client, router_app, router_module,
) -> None:
    router_module._REGISTRY.register("proj", "/tmp/ws-new-mcp-test")
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"}
    )
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/proj",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer tok-1234",
            "Content-Type": "application/json",
        },
    )

    # 504 (backend not running) is fine — proves the route resolves.
    assert resp.status != 404
