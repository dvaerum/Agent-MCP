"""R8-F2: SSE (`GET /mcp`) proxy must STREAM, not buffer, and tie the
upstream lifetime to the downstream client + cap concurrency.

The backend's ``GET /mcp`` handler returns an INFINITE
``text/event-stream`` (a ``: ping`` heartbeat forever). The router's
reverse proxy used to ``await up.read()`` the whole upstream body
before replying — which for an infinite stream NEVER returns:

  * the client never sees response headers/frames;
  * the router→backend UNIX-socket connection stays ESTABLISHED for
    the agent-bearer's whole lifetime even after the client drops;
  * ``_track_connection`` never decrements ``active_conns``, so the
    idle reaper (which requires ``active_conns == 0``) can never stop
    the backend — ONE abandoned SSE pins a backend forever;
  * there is NO cap, so any valid bearer can open unbounded streams.

These tests pin the fix at the testable seams:

  1. A ``text/event-stream`` upstream is forwarded INCREMENTALLY
     (StreamResponse, not a buffered ``web.Response``) — proven by the
     client receiving frames while the upstream is still open.
  2. A client disconnect tears the upstream down: ``active_conns``
     returns to 0 so the reaper works again.
  3. Concurrent SSE proxies are capped per-agent: the over-limit one
     is rejected with a clean 429 (no hang), and the live-stream count
     stays bounded.

The full end-to-end leak (backend UDS conns returning to baseline
after an 8-concurrent-SSE burst) is a live-proxy behaviour re-measured
on the VM (RE_VERIFY); these seam tests pin the code paths that fix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


# ── Infinite-SSE backend stand-in ───────────────────────────────────


class _StreamingBackend:
    """UDS-bound aiohttp app whose ``GET /mcp`` emits an unbounded
    ``text/event-stream`` — two real frames, then a fast ``: ping``
    heartbeat loop forever (models ``main_app._handle_get``, just with
    a sub-second interval so a test doesn't wait 15 s per heartbeat).

    ``handler_exited`` is set when the backend handler returns — i.e.
    when the router closed the upstream connection (the observable
    proof that a client disconnect propagated all the way through).
    """

    HEARTBEAT_SEC = 0.02

    def __init__(self) -> None:
        self.handler_exited = asyncio.Event()
        self.get_started = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        await req.read()
        if req.method != "GET":
            return web.Response(body=b"OK")
        self.get_started.set()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(req)
        try:
            await resp.write(b"data: frame-1\n\n")
            await resp.write(b"data: frame-2\n\n")
            # Infinite heartbeat — never terminates on its own.
            while True:
                await asyncio.sleep(self.HEARTBEAT_SEC)
                await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            # Router closed the upstream (client disconnected). Fall
            # through to signal the handler is unwinding.
            pass
        finally:
            self.handler_exited.set()
        return resp


async def _start_backend_on_uds(
    backend: _StreamingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def streaming_backend(router_module, router_env, systemctl_stub):
    """Infinite-SSE backend for project 'proj', pre-registered + active,
    with the agent-token cache seeded so a bearer clears the router
    edge without a real ``/api/tokens`` round-trip."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._agent_token_cache["proj"] = (9.9e18, {"tok-1234": "Admin"})
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _StreamingBackend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


async def _poll_until(pred, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll ``pred`` until true or timeout; return the final truthiness."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


# ── Tests ───────────────────────────────────────────────────────────


async def test_sse_upstream_is_streamed_not_buffered(
    aiohttp_client, router_app, streaming_backend, router_module,
) -> None:
    """A ``GET /mcp`` whose upstream is an infinite ``text/event-stream``
    must reach the client INCREMENTALLY.

    On the buffered code path the router blocks on ``await up.read()``
    of the never-ending upstream, so response headers never reach the
    client and ``client.get(...)`` hangs — this test then fails as a
    timeout (RED). The streaming fix sends headers + frames as they
    arrive.
    """
    client = await aiohttp_client(router_app)

    # Headers must arrive without the (infinite) body completing.
    resp = await asyncio.wait_for(
        client.get(
            "/agent-mcp/mcp/proj",
            headers={"Authorization": "Bearer tok-1234"},
        ),
        timeout=4.0,
    )
    try:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        # First frame must be readable while the upstream is still open.
        frame = await asyncio.wait_for(
            resp.content.readuntil(b"\n\n"), timeout=4.0,
        )
        assert frame == b"data: frame-1\n\n"
    finally:
        resp.close()


async def test_client_disconnect_tears_down_upstream(
    aiohttp_client, router_app, streaming_backend, router_module,
) -> None:
    """When the SSE client disconnects, the router→backend upstream is
    released and ``active_conns`` returns to 0 so the idle reaper works.
    """
    _po = router_module._po
    client = await aiohttp_client(router_app)

    resp = await asyncio.wait_for(
        client.get(
            "/agent-mcp/mcp/proj",
            headers={"Authorization": "Bearer tok-1234"},
        ),
        timeout=4.0,
    )
    # Drain the first frame so we know the stream is live end-to-end.
    await asyncio.wait_for(resp.content.readuntil(b"\n\n"), timeout=4.0)
    assert _po.active_conns["proj"] == 1, "stream should count as one conn"

    # Client goes away.
    resp.close()

    # The upstream must be released → active_conns back to 0, and the
    # backend handler must observe its stream close.
    assert await _poll_until(lambda: _po.active_conns["proj"] == 0), (
        "active_conns did not return to 0 after client disconnect — the "
        "orphaned upstream leaked and will pin the backend from the reaper"
    )
    assert await _poll_until(streaming_backend.handler_exited.is_set), (
        "backend SSE handler never saw its upstream close"
    )


async def test_concurrent_sse_capped_per_agent(
    aiohttp_client, router_app, streaming_backend, router_module,
    monkeypatch,
) -> None:
    """More than ``MAX_STREAMS_PER_AGENT`` concurrent SSE proxies for one
    bearer → the over-limit request is rejected with a clean 429 (no
    hang), and the live-stream count stays at the cap.
    """
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 2)
    client = await aiohttp_client(router_app)

    held = []

    async def _open_stream():
        r = await asyncio.wait_for(
            client.get(
                "/agent-mcp/mcp/proj",
                headers={"Authorization": "Bearer tok-1234"},
            ),
            timeout=4.0,
        )
        # Read one frame so the stream is fully established (past the
        # cap admission point).
        await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=4.0)
        return r

    try:
        held.append(await _open_stream())
        held.append(await _open_stream())
        # Two streams admitted; the live count is exactly the cap.
        assert _po._streaming_proxies_global == 2

        # The third must be rejected cleanly, not hang.
        rejected = await asyncio.wait_for(
            client.get(
                "/agent-mcp/mcp/proj",
                headers={"Authorization": "Bearer tok-1234"},
            ),
            timeout=4.0,
        )
        assert rejected.status == 429
        rejected.close()
        # Still exactly the cap — the rejected one didn't leak a stream.
        assert _po._streaming_proxies_global == 2
    finally:
        for r in held:
            r.close()
