"""Regression: record clientInfo even when the client disconnects after
the body (the production `httptools`-uvicorn / router-proxy path).

The event-loop token-saving hold keys on the `clientInfo.name` recorded
at the MCP `initialize` handshake. `_McpAsgiApp.__call__` drains the
request body up front (for the depth-guard + stateless replay). If
`_drain_body` returns `disconnected=True` — which a `httptools`-based
uvicorn surfaces when the client (or the router's short-lived proxied
connection) hangs up immediately after sending the body — the OLD code
skipped `_maybe_record_client_info`, so `get_client_name()` stayed empty
and every agent fell to the 55s no-heartbeat re-poll instead of the
parked heartbeat hold. Recording must happen off the already-drained
body regardless of the disconnect signal.
"""

from __future__ import annotations

import json

import pytest


class _FakeManager:
    """Minimal stand-in for the StreamableHTTP session manager: drains
    the (replayed) receive and sends nothing of consequence."""

    async def handle_request(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                break
            if not msg.get("more_body", False):
                break


@pytest.mark.asyncio
async def test_records_clientinfo_when_client_disconnects_after_body(monkeypatch):
    from agent_mcp.app import main_app
    from agent_mcp.core import client_info_registry as reg

    # Isolate from the DB: any bearer resolves to a fixed agent_id.
    monkeypatch.setattr(main_app, "get_agent_id", lambda t: "dc-agent" if t else None)

    app = main_app._McpAsgiApp(_FakeManager())

    init = json.dumps(
        {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "2.1.207"},
            },
        }
    ).encode()

    # The pathological ordering: body arrives with more_body=True, then an
    # http.disconnect BEFORE a more_body=False terminator → _drain_body
    # returns (body, disconnected=True).
    messages = [
        {"type": "http.request", "body": init, "more_body": True},
        {"type": "http.disconnect"},
    ]
    it = iter(messages)

    async def receive():
        try:
            return next(it)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"authorization", b"Bearer tok-abc")],
    }

    reg.clear()
    await app(scope, receive, send)

    assert reg.get_client_name("dc-agent") == "claude-code", (
        "clientInfo must be recorded even when the client disconnects after "
        "sending the body — otherwise the event-loop hold-strategy resolver "
        "never learns the client identity"
    )
