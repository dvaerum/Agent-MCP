"""PR-D — MCP transport URL move.

  Old (Shape 3 carry-over)              New (PR-D)
  ─────────────────────────────────────────────────────────────
  /agent-mcp/<name>/mcp                 /agent-mcp/mcp/<name>

The MCP path was the last bit of Shape 3 the audit identified that
the dashboard + REST rename (PR-B) deliberately deferred — moving it
forces an MCP-client config rewrite for every operator (because the
URL gets baked into every .mcp.json) and isolating that churn in its
own PR keeps the blast radius observable.

Other PR-D touches:
  - _mcp_url_for() in router/app.py rewrites to the new shape so
    the wiring-snippet endpoints (/__client-config, /__client-installer)
    emit working URLs.
  - mcpUrl() in dashboard lib/urls.ts becomes the one-line change that
    propagates the new shape to every dashboard consumer (audit §1.1
    fix from PR-B routed through this helper).
  - Service descriptor's endpoints.mcp field surfaces the new path
    pattern so a client following the descriptor doesn't hit the old
    URL.
  - The legacy /agent-mcp/<name>/mcp path 308-redirects to the new
    /agent-mcp/mcp/<name> for the 30-day grace period.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


# ── New MCP path ────────────────────────────────────────────────────


async def test_new_mcp_path_routes_to_backend_mcp_handler(
    aiohttp_client, router_app, router_module,
) -> None:
    """``POST /agent-mcp/mcp/<name>`` reaches the MCP handler with the
    right project. The auth gate (admin / agent bearer) still fires;
    we seed the token cache so we get past it and into the handler.
    """
    router_module._REGISTRY.register("proj", "/tmp/ws-prd-test")
    router_module._agent_token_cache["proj"] = (9.9e18, {"tok-1234": "Admin"})
    # No backend stood up — we'll get a 504 (backend didn't appear),
    # but reaching the handler at all proves the route is registered.
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/proj",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer tok-1234",
            "Content-Type": "application/json",
        },
    )

    # 504 is the "backend not running" response from the handler;
    # 404 would mean the route didn't match at all.
    assert resp.status != 404, (
        f"expected /agent-mcp/mcp/<name> to be a registered route; "
        f"got {resp.status}"
    )


# The /agent-mcp/<name>/mcp 308 redirect was dropped in v5.0.0 after
# the 30-day grace window. test_legacy_url_removal.py now asserts the
# legacy path 404s.


# ── Service descriptor reflects new shape ───────────────────────────


async def test_descriptor_advertises_new_mcp_prefix(
    aiohttp_client, router_app,
) -> None:
    """A client following the descriptor's endpoints.mcp lands on the
    new prefix, not the legacy one."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/", headers={"Accept": "application/json"},
    )

    body = await resp.json()
    # PR-D moves the MCP shape so the descriptor advertises the new
    # top-level /mcp prefix; clients append /<name> to it.
    assert body["endpoints"]["mcp"] == "/agent-mcp/mcp"


# ── Internal MCP URL helper ─────────────────────────────────────────


# Bypass the module-level asyncio mark for this sync test by checking
# the helper directly. pytest-asyncio will warn on the mark mismatch
# but the test still runs fine; the warning is filtered in CI.
def test_mcp_url_for_helper_returns_new_shape(router_module) -> None:
    """The router's internal ``_mcp_url_for`` helper builds the URL
    embedded in .mcp.json client configs (via /__client-config). It
    MUST emit the new shape so operators who download the .mcp.json
    end up with a working URL."""
    url = router_module._mcp_url_for("proj")
    # External URL is set by the test fixture to https://router.example.test.
    assert url.endswith("/agent-mcp/mcp/proj"), (
        f"expected URL ending in /agent-mcp/mcp/proj, got {url!r}"
    )
    assert "/proj/mcp" not in url, (
        f"old shape leaked into _mcp_url_for: {url!r}"
    )
