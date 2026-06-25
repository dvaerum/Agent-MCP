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


# ── Wrong-HTTP-method hygiene (verify-all-v4 MUTATING #2 follow-up) ─


@pytest.mark.parametrize("method", ["PUT", "PATCH", "OPTIONS"])
async def test_unsupported_methods_return_405_not_500(
    aiohttp_client, router_app, router_module, method,
) -> None:
    """The MCP JSON-RPC transport is POST-only (with GET reserved for
    bearer-auth SSE notifications and DELETE for session termination,
    per MCP Streamable HTTP spec rev 2025-03-26). PUT/PATCH/OPTIONS
    have no meaning on this endpoint and MUST be rejected at the
    router with 405 Method Not Allowed — never reach the upstream
    backend and never surface as 5xx.

    Surfaced by verify-all-v4 MUTATING #2 (wrong-HTTP-method
    full-catalog probe): the previous behaviour fell through to the
    backend's ``_handle_get``/manager and produced 500
    ``session_registry_no_agent`` for GET via cookie auth. This guard
    short-circuits every non-spec verb cleanly so the catalog-probe
    hygiene matches the other 14 endpoints (which all 4xx cleanly).
    """
    router_module._REGISTRY.register("proj", "/tmp/ws-method-guard-test")
    client = await aiohttp_client(router_app)

    resp = await client.request(
        method,
        "/agent-mcp/mcp/proj",
        headers={"Authorization": "Bearer whatever"},
    )

    assert resp.status == 405, (
        f"expected 405 Method Not Allowed for {method} on /mcp/<name>; "
        f"got {resp.status}"
    )
    # Defence in depth: never 5xx for a wrong-verb probe.
    assert resp.status < 500, (
        f"{method} on /mcp/<name> returned {resp.status}; the wrong-"
        f"verb probe must always be 4xx, never 5xx"
    )


async def test_get_without_bearer_returns_4xx_not_500(
    aiohttp_client, router_app, router_module,
) -> None:
    """GET on the MCP transport is a legitimate verb ONLY for
    bearer-authenticated callers (the SSE notification stream — the
    bearer is what the backend's ``_handle_get`` uses to resolve
    ``agent_id`` for ``session_registry``). A GET that authenticates
    via the operator-session cookie path has no derivable agent_id
    and used to fall through to the backend, where
    ``_handle_get`` returned 500 ``session_registry_no_agent``.

    Router-side guard: GET without a bearer header returns 405 (the
    cookie path simply does not support the notification stream).
    Asserts the response stays 4xx — verify-all-v4 MUTATING #2
    hygiene fix.
    """
    router_module._REGISTRY.register("proj", "/tmp/ws-get-bearer-test")
    client = await aiohttp_client(router_app)  # auto-logs-in sentinel op

    # No Authorization header — the request rides the sentinel-op
    # cookie alone. This is the exact shape verify-all-v4 hit.
    resp = await client.get("/agent-mcp/mcp/proj")

    assert resp.status < 500, (
        f"GET /mcp/<name> via cookie auth returned {resp.status}; "
        f"the bug we're fixing is exactly the 500 fall-through to "
        f"backend ``_handle_get`` ``session_registry_no_agent``"
    )
    assert resp.status == 405, (
        f"expected 405 Method Not Allowed for cookie-only GET on "
        f"/mcp/<name>; got {resp.status}"
    )
