"""Tests for the bare ``/agent-mcp/app/`` route added in
Phase 3.5a to serve the React overview page (no project segment).

The router serves the same on-disk Next.js index.html as it does for
``/agent-mcp/app/<name>/``; the dashboard JS reads its
mode from ``window.location.pathname`` and renders the overview when
the project segment is missing.

These tests pin:

1. ``GET /agent-mcp/app/`` resolves to a 200 serving the
   static export's index.html.
2. ``GET /agent-mcp/app`` (no trailing slash) redirects to
   the canonical trailing-slash form.
3. Nested overview paths under ``/app/?<query>`` fall through
   to the same SPA fallback (in case the overview later grows
   subroutes).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_bare_dashboard_serves_index_html(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("index.html", "<html><body>SPA root</body></html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/")

    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    body = await resp.text()
    assert "SPA root" in body


async def test_bare_dashboard_no_slash_redirects(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app", allow_redirects=False)

    assert resp.status in (301, 302, 308)
    assert resp.headers["Location"] == "/agent-mcp/app/"


async def test_bare_dashboard_with_query_preserved(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    write_dashboard_file("index.html", "<html>SPA</html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/?page=overview")

    assert resp.status == 200
