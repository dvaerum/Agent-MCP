"""URL → handler routing for the public router surface.

These tests are deliberately shallow: they prove each declared route
is reachable and dispatched to the right handler. Handler behaviour
is asserted in the targeted ``test_*`` modules (SPA fallback, proxy
passthrough, 410 migration, etc.).

We exercise routes via ``aiohttp_client`` so the full aiohttp router
+ middleware stack is in the loop — introspecting ``app.router``
directly would miss aiohttp's own redirect / trailing-slash quirks.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_projects_endpoint_returns_json_list(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__projects")

    assert resp.status == 200
    body = await resp.json()
    assert body == {"projects": ["alpha", "beta"]}
    # The dashboard hits this on every project-picker render; it must
    # never be cached.
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_dashboard_with_trailing_slash_resolves(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``/agent-mcp/__dashboard/foo/`` must hit the dashboard handler
    even if no ``foo`` project exists — the handler ignores ``name``
    today (one on-disk tree serves every project)."""
    write_dashboard_file("index.html", "<html>root</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo/")

    assert resp.status == 200
    assert "root" in await resp.text()


async def test_dashboard_nested_path_resolves(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Nested path with no on-disk file falls through to SPA fallback,
    which the next test pins more carefully. Here we only assert the
    route resolved (no 404 from aiohttp's own dispatcher)."""
    write_dashboard_file("index.html", "<html>root</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo/tasks")

    # 200 from SPA fallback; the targeted SPA test covers the body.
    assert resp.status == 200


async def test_dashboard_bare_redirects_to_trailing_slash(
    aiohttp_client, router_app,
) -> None:
    """No trailing slash → 301 to the canonical trailing-slash form.

    aiohttp's test client follows redirects by default; disable that
    so we can inspect the 301 itself.
    """
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo", allow_redirects=False)

    assert resp.status == 301
    assert resp.headers["Location"] == "/agent-mcp/__dashboard/foo/"


async def test_create_accepts_form_encoded_name(
    aiohttp_client, router_app,
) -> None:
    """``POST /agent-mcp/__create`` accepts ``name=<slug>`` form bodies
    and returns a 303 redirect to the index page (HTTPSeeOther)."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__create",
        data={"name": "newproj"},
        allow_redirects=False,
    )

    assert resp.status == 303
    location = resp.headers["Location"]
    assert location.startswith("/agent-mcp/?")
    assert "created=newproj" in location


async def test_legacy_sse_returns_410(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/__sse/<name>`` was retired in Agent-MCP 3.0.0.
    The router replies 410 with a structured JSON migration body so
    any client/config still pointed at it gets a parseable hint."""
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__sse/foo")

    assert resp.status == 410
    assert resp.headers["Content-Type"].startswith("application/json")
    body = await resp.json()
    assert body["error"] == "endpoint_removed"
    assert body["migrated_to"] == "/mcp"


async def test_unknown_route_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__nonsense-no-such-route")

    assert resp.status == 404
