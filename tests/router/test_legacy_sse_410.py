"""Pin the 410 Gone responses for retired SSE-era endpoints.

dvaerum/Agent-MCP 3.0.0 dropped the SSE+messages transport pair in
favour of a single ``POST/GET/DELETE /mcp`` (Streamable HTTP, MCP
spec rev 2025-03-26). The router exposes the new shape at
``/agent-mcp/<name>/mcp`` and 410s the old shapes so any
client/config still pointed at them gets a structured, parseable
hint pointing at the new URL.

These are simple pin tests — they don't exercise any logic the
``test_url_routing.py`` smoke for /__sse/ doesn't already touch — but
they pin the JSON body shape (``error``, ``migrated_to``, ``hint``)
that downstream tooling parses to guide users to the new URL.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


def _assert_migration_body(body: dict) -> None:
    assert body["error"] == "endpoint_removed"
    assert body["migrated_to"] == "/mcp"
    assert body["spec_revision"] == "2025-03-26"
    assert "Authorization: Bearer" in body["hint"]
    # The hint must point at the new /mcp URL shape so a confused
    # operator copy-pasting a token sees where it goes.
    assert "/agent-mcp/<name>/mcp" in body["hint"]


async def test_get_legacy_sse_returns_410_with_migration_body(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/__sse/foo")

    assert resp.status == 410
    assert resp.headers["Content-Type"].startswith("application/json")
    _assert_migration_body(await resp.json())


async def test_post_legacy_sse_also_returns_410(
    aiohttp_client, router_app,
) -> None:
    """Route is declared with method ``*`` — POST should hit it too."""
    client = await aiohttp_client(router_app)

    resp = await client.post("/agent-mcp/__sse/foo", data=b"")

    assert resp.status == 410
    _assert_migration_body(await resp.json())


async def test_post_legacy_messages_returns_410(
    aiohttp_client, router_app,
) -> None:
    """``POST /agent-mcp/__messages/<name>/<rest>`` — the paired
    handshake target — also retired in 3.0.0."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/__messages/foo/some/path",
        data=b'{"jsonrpc":"2.0","id":1,"method":"x"}',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 410
    assert resp.headers["Content-Type"].startswith("application/json")
    _assert_migration_body(await resp.json())
