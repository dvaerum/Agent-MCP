"""Regression test for commit c1d85d9 (router: dashboard SPA fallback).

The Next.js static export ships exactly one ``index.html`` — every
client-side route (``/tasks``, ``/agents``, ...) is resolved by the
React router after the bundle boots. The router has to serve the root
``index.html`` for any unknown nested path under
``/agent-mcp/__dashboard/<name>/`` rather than 404-ing, otherwise a
browser refresh on a deep link wipes the page.

If this test goes red, deep-link reloads break for every dashboard
user. Don't relax the assertion — fix the regression.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_SENTINEL = "SPA-ROOT-INDEX-MARKER"
_INDEX_HTML = f"<!doctype html><html><body>{_SENTINEL}</body></html>"


async def test_unknown_dashboard_path_serves_root_index(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``/agent-mcp/__dashboard/<name>/tasks/`` has no matching file on
    disk — must fall back to the root ``index.html`` so the SPA's
    React-router can render the section client-side."""
    write_dashboard_file("index.html", _INDEX_HTML)
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo/tasks/")

    assert resp.status == 200
    body = await resp.text()
    assert _SENTINEL in body, (
        "SPA fallback regressed: unknown nested path did not serve "
        "the root index.html. See commit c1d85d9."
    )


async def test_unknown_dashboard_path_no_trailing_slash_also_falls_back(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``/agent-mcp/__dashboard/<name>/tasks`` (no trailing slash on
    the section) is the common bookmarked form. It must also serve
    the SPA index — the bare-name-only redirect is targeted at
    ``/agent-mcp/__dashboard/<name>`` (no nested path) and must not
    fire here.
    """
    write_dashboard_file("index.html", _INDEX_HTML)
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/foo/tasks", allow_redirects=False,
    )

    assert resp.status == 200, (
        f"expected 200 with SPA fallback, got {resp.status} — the "
        "bare-name 301 redirect may be over-matching"
    )
    body = await resp.text()
    assert _SENTINEL in body


async def test_dashboard_serves_real_file_when_present(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """If a real file exists at the requested path, serve THAT — the
    SPA fallback is a last resort, not a blanket index-replacement."""
    write_dashboard_file("index.html", _INDEX_HTML)
    write_dashboard_file("static-asset.txt", "REAL-FILE-CONTENT")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo/static-asset.txt")

    assert resp.status == 200
    assert (await resp.text()) == "REAL-FILE-CONTENT"


async def test_spa_fallback_404s_when_no_index_html(
    aiohttp_client, router_app,
) -> None:
    """Edge case: if even the root ``index.html`` is missing (broken
    deploy), 404 rather than blowing up with a 500."""
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__dashboard/foo/tasks/")

    assert resp.status == 404
