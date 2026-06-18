"""URL → handler routing for the public router surface.

These tests are deliberately shallow: they prove each declared route
is reachable and dispatched to the right handler. Handler behaviour
is asserted in the targeted ``test_*`` modules (SPA fallback, proxy
passthrough, etc.).

We exercise routes via ``aiohttp_client`` so the full aiohttp router
+ middleware stack is in the loop — introspecting ``app.router``
directly would miss aiohttp's own redirect / trailing-slash quirks.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


async def test_projects_endpoint_returns_json_list(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body == {"projects": ["alpha", "beta"]}
    # The dashboard hits this on every project-picker render; it must
    # never be cached.
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_dashboard_with_trailing_slash_resolves(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``/agent-mcp/app/foo/`` (PR-B renamed from /__dashboard/) must
    hit the dashboard handler
    even if no ``foo`` project exists — the handler ignores ``name``
    today (one on-disk tree serves every project)."""
    write_dashboard_file("index.html", "<html>root</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/foo/")

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

    resp = await client.get("/agent-mcp/app/foo/tasks")

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

    resp = await client.get("/agent-mcp/app/foo", allow_redirects=False)

    assert resp.status == 301
    assert resp.headers["Location"] == "/agent-mcp/app/foo/"


async def test_create_accepts_json_post(
    aiohttp_client, router_app,
) -> None:
    """``POST /agent-mcp/api/router/projects`` accepts a JSON body
    ``{"name": "<slug>"}`` and returns 201 with the unified envelope."""
    import json
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "newproj"}),
        headers={**_STRICT_ACCEPT, "Content-Type": "application/json"},
        allow_redirects=False,
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["success"] is True
    assert body["project"]["name"] == "newproj"


async def test_legacy_sse_returns_404(
    aiohttp_client, router_app,
) -> None:
    """``GET /agent-mcp/__sse/<name>`` was retired in Agent-MCP 3.0.0
    and ran as a 410-Gone handler with a JSON migration body through
    Phase 5 of the router-upstream plan. Phase 6 deleted the handler;
    the URL now 404s via aiohttp's default behaviour. The intent is
    that any client still configured for SSE fails hard rather than
    receiving a structured migration hint indefinitely."""
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__sse/foo")

    assert resp.status == 404


async def test_legacy_messages_returns_404(
    aiohttp_client, router_app,
) -> None:
    """Same retirement story as `__sse`: the paired
    ``/agent-mcp/__messages/<name>/<rest>`` POST endpoint also 404s
    after Phase 6 cleanup."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__messages/foo/some/path",
        data=b'{"jsonrpc":"2.0","id":1,"method":"x"}',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 404


async def test_unknown_route_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__nonsense-no-such-route")

    assert resp.status == 404
