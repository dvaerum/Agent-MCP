"""Regression test for commit ffd1c4a (router: 301 bare-name
dashboard URL to canonical trailing-slash form).

Why this matters: the dashboard's index.html embeds asset URLs
relative to the request path. Without a trailing slash on
``/agent-mcp/__dashboard/<name>``, the browser resolves
``<script src="_next/static/...">`` against ``/agent-mcp/__dashboard``
(the bare segment) instead of ``/agent-mcp/__dashboard/<name>/``,
fetches give 404, the page renders blank.

aiohttp's own automatic trailing-slash matching was off the table —
it only kicks in for paths without dynamic segments. The fix was an
explicit redirect route. This test pins both arms:

  1. ``GET /agent-mcp/__dashboard/<name>`` returns 301 to
     ``/agent-mcp/__dashboard/<name>/`` (with the slash).
  2. ``GET /agent-mcp/__dashboard/<name>/<section>`` (no trailing
     slash on the section) is handled by the dashboard handler /
     SPA fallback — NOT by the redirect. Pinning this arm is what
     would have caught a botched fix that scoped the redirect too
     widely.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_INDEX_HTML = "<!doctype html><html><body>SPA-INDEX</body></html>"


async def test_bare_dashboard_name_redirects_to_trailing_slash(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/myproj", allow_redirects=False,
    )

    assert resp.status == 301, (
        f"expected 301, got {resp.status} — bare-name dashboard URL "
        "must redirect to the canonical trailing-slash form so the "
        "embedded relative asset URLs in index.html resolve correctly"
    )
    assert resp.headers["Location"] == "/agent-mcp/__dashboard/myproj/"


async def test_bare_dashboard_section_does_not_redirect(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``/agent-mcp/__dashboard/myproj/tasks`` (one segment past the
    project name, no trailing slash) must NOT 301 — it's a routing
    target handled by the dashboard handler's SPA fallback. A 301
    here would mean the redirect is scoped too widely and is eating
    every dashboard request.
    """
    write_dashboard_file("index.html", _INDEX_HTML)
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/__dashboard/myproj/tasks", allow_redirects=False,
    )

    assert resp.status == 200, (
        f"expected 200 SPA fallback, got {resp.status} — the bare-name "
        "redirect's path scope is leaking into deeper paths"
    )
    assert "SPA-INDEX" in await resp.text()


async def test_bare_dashboard_redirect_uses_match_info_name(
    aiohttp_client, router_app,
) -> None:
    """The redirect target must echo the requested project name, not
    a hardcoded one. This is the boring test that catches a copy-paste
    error in the lambda factory.
    """
    client = await aiohttp_client(router_app)

    for name in ("alpha", "b", "long-hyphenated-name"):
        resp = await client.get(
            f"/agent-mcp/__dashboard/{name}", allow_redirects=False,
        )
        assert resp.status == 301
        assert resp.headers["Location"] == f"/agent-mcp/__dashboard/{name}/"
