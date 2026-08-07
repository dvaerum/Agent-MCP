"""Backend SSE endpoints treat a peer disconnect as normal termination.

Sibling coverage for the router-side fix in
``tests/router/test_sse_client_disconnect_quiet.py``. The bug class is
"a streaming response path that treats a client disconnect as an
application error" — an ERROR traceback for a closed browser tab, which
masks genuine failures. The router proxies these two backend streams, so
they belong to the same class and are pinned here rather than assumed:

  * ``GET /api/events`` — the operator dashboard's live-update channel.
  * ``GET /api/delivery/stream`` — the ADR-0021 per-worker push channel.

Both are built on ``sse_starlette.EventSourceResponse``, whose
``_listen_for_disconnect`` cancels the whole task group on
``http.disconnect``. These tests drive the ASGI app directly (a real
``http.disconnect`` event, no server needed) and assert the contract we
actually depend on: the request returns normally — no exception for the
ASGI server to log — and the generator's ``finally`` ran, so the
subscriber is unregistered instead of leaking for the process's life.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_mcp.app.deps import require_operator_session
from agent_mcp.features import delivery_transport as dt
from agent_mcp.features import operator_events
from tests.harness import mcp_session


def _get_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 45678),
        "server": ("testserver", 80),
    }


async def _stream_then_disconnect(app, scope: dict[str, Any]) -> list[dict]:
    """Run ``app`` for ``scope``, hanging up as soon as it responds.

    ``receive`` blocks until the response has started, then delivers a
    single ``http.disconnect`` — the exact event an ASGI server emits
    when the peer's socket goes away. Returns the messages the app sent
    so the caller can assert the stream really opened first.
    """
    sent: list[dict] = []
    responded = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await responded.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        responded.set()

    # No try/except: an exception escaping here IS the bug — it is what
    # the ASGI server would render as an application-error traceback.
    await asyncio.wait_for(app(scope, receive, send), timeout=10.0)
    return sent


@pytest.mark.asyncio
async def test_operator_events_peer_disconnect_unsubscribes_quietly(
    tmp_path: Path,
) -> None:
    """A dashboard tab closing its live-update SSE must end the request
    normally and release its subscriber slot."""
    async with mcp_session(tmp_path) as admin:
        app = admin.client.app
        app.dependency_overrides[require_operator_session] = lambda: {
            "operator_id": "op-under-test",
        }
        try:
            before = operator_events.subscriber_count()
            sent = await _stream_then_disconnect(
                app,
                _get_scope("/api/events", [(b"accept", b"text/event-stream")]),
            )
        finally:
            app.dependency_overrides.pop(require_operator_session, None)

    assert sent and sent[0]["type"] == "http.response.start", (
        f"stream never opened: {sent!r}"
    )
    assert sent[0]["status"] == 200
    assert operator_events.subscriber_count() == before, (
        "peer disconnect left the operator_events subscriber registered — "
        "the stream's finally never ran"
    )


@pytest.mark.asyncio
async def test_delivery_stream_peer_disconnect_unsubscribes_quietly(
    tmp_path: Path,
) -> None:
    """Same contract for the ADR-0021 worker delivery stream: hanging up
    ends the request normally and disconnects the worker's transport."""
    dt.clear()
    try:
        async with mcp_session(tmp_path) as admin:
            app = admin.client.app
            from agent_mcp.core.auth import get_agent_id

            agent_id = get_agent_id(admin.admin_token)
            assert agent_id, "harness bearer must resolve to an agent"

            sent = await _stream_then_disconnect(
                app,
                _get_scope(
                    "/api/delivery/stream",
                    [
                        (b"accept", b"text/event-stream"),
                        (
                            b"authorization",
                            f"Bearer {admin.admin_token}".encode(),
                        ),
                    ],
                ),
            )

        assert sent and sent[0]["type"] == "http.response.start", (
            f"stream never opened: {sent!r}"
        )
        assert sent[0]["status"] == 200
        assert dt.is_connected(agent_id) is False, (
            "peer disconnect left the delivery transport registered as "
            "connected — the stream's finally never ran"
        )
    finally:
        dt.clear()
