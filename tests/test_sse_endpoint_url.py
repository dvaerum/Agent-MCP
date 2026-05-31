"""Regression: the SSE handshake's `endpoint` event must announce
`/messages/...`, not `/sse/messages/...`.

Background. UPSTREAM_ISSUES.md issue A's fix (PR #25) switched `/sse`
from `Route('/sse', endpoint=...)` to
`Mount('/sse', app=SseConnectApp())`. That fixed the `TypeError:
'NoneType' object is not callable` crash, but introduced a URL-shape
regression: Starlette's `Mount` rewrites the request scope so
`scope['root_path'] = '/sse'` (and `scope['path']` strips the prefix).
MCP's `SseServerTransport.connect_sse` then computes the URL it tells
the client to POST follow-up JSON-RPC to as

    root_path.rstrip("/") + self._endpoint

(see `mcp/server/sse.py:152-158`). With `root_path='/sse'` and
`_endpoint='/messages/'` it emits `data: /sse/messages/?session_id=...`
instead of the expected `data: /messages/?session_id=...`.

Anything in front of the backend that rewrites `data: /messages/`
on the SSE byte stream — e.g. a multi-tenant router proxying
`/agent-mcp/<name>/messages/` to the per-project backend — stops
matching. Clients then POST to `/sse/messages/?session_id=...` (or
its prefixed form) which 404s. Multi-session deployments are broken
even though the original `/sse` GET works.

Fix: in `SseConnectApp.__call__`, strip `root_path` from the scope
before invoking the SSE handler so the transport computes the URL as
if mounted at root.

This test invokes the wrapper in isolation with a monkey-patched SSE
handler so it can inspect the scope the wrapper passed through, rather
than spinning up the full app and streaming real SSE bytes.
"""

from __future__ import annotations

import asyncio

import pytest


def test_sse_connect_app_strips_mount_root_path(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SseConnectApp` must zero out `scope['root_path']` before
    invoking the SSE handler.

    Without this, MCP's `SseServerTransport.connect_sse` builds the
    follow-up POST URL it advertises to the client as
    `root_path.rstrip('/') + self._endpoint` (mcp/server/sse.py:152),
    producing `/sse/messages/?session_id=...` instead of
    `/messages/?session_id=...`. Anything proxying the SSE byte stream
    on its way to the client (e.g. a multi-tenant router rewriting
    `/messages/` → `/agent-mcp/<name>/messages/`) stops matching, and
    clients POST follow-up JSON-RPC to a URL that 404s.
    """
    from agent_mcp.app import main_app
    from starlette.routing import Mount

    captured: dict[str, object] = {}

    async def fake_handler(request):  # type: ignore[no-untyped-def]
        captured["root_path"] = request.scope.get("root_path")
        captured["path"] = request.scope.get("path")

    monkeypatch.setattr(main_app, "sse_connection_handler", fake_handler)

    sse_mount = next(
        r for r in app.routes
        if isinstance(r, Mount) and getattr(r, "path", None) == "/sse"
    )

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/",            # Mount stripped /sse off the front
        "raw_path": b"/",
        "root_path": "/sse",    # Mount set this
        "query_string": b"",
        "headers": [],
        "app": app,
    }

    async def noop_receive():
        return {"type": "http.disconnect"}

    async def noop_send(_message):
        return None

    asyncio.run(sse_mount.app(scope, noop_receive, noop_send))

    assert captured.get("root_path") == "", (
        f"SseConnectApp must clear scope['root_path'] before calling the "
        f"SSE handler, otherwise MCP's transport prepends it to /messages/ "
        f"when telling the client where to POST. "
        f"got root_path={captured.get('root_path')!r}"
    )
