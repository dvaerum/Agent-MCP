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
import contextlib
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

    def __init__(self, heartbeat_sec: float | None = None) -> None:
        self.handler_exited = asyncio.Event()
        self.get_started = asyncio.Event()
        # Per-instance override so a test can make the backend go QUIET
        # after the initial frames (a long heartbeat ≈ no further writes
        # for the whole test window), isolating the router's own
        # disconnect detection from any write-triggered teardown.
        self.heartbeat_sec = (
            heartbeat_sec if heartbeat_sec is not None else self.HEARTBEAT_SEC
        )
        # Count of heartbeat writes emitted AFTER the two opening frames.
        # A disconnect test asserts this is still 0 when the slot frees,
        # proving teardown happened without waiting for a subsequent
        # upstream write.
        self.heartbeats_written = 0

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
                await asyncio.sleep(self.heartbeat_sec)
                await resp.write(b": ping\n\n")
                self.heartbeats_written += 1
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


async def _make_streaming_backend(
    router_module, router_env, systemctl_stub, heartbeat_sec=None,
):
    """Register project 'proj', seed its bearer, and start an infinite-SSE
    backend on its UDS. Returns ``(backend, runner)`` — caller cleans up
    the runner."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._agent_token_cache["proj"] = (9.9e18, {"tok-1234": "Admin"})
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _StreamingBackend(heartbeat_sec=heartbeat_sec)
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    return backend, runner


@pytest_asyncio.fixture
async def streaming_backend(router_module, router_env, systemctl_stub):
    """Infinite-SSE backend for project 'proj', pre-registered + active,
    with the agent-token cache seeded so a bearer clears the router
    edge without a real ``/api/tokens`` round-trip."""
    backend, runner = await _make_streaming_backend(
        router_module, router_env, systemctl_stub,
    )
    try:
        yield backend
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def quiet_streaming_backend(router_module, router_env, systemctl_stub):
    """Like ``streaming_backend`` but goes QUIET after the two opening
    frames — a heartbeat far longer than any test window, so the backend
    emits NO further writes. Used to prove the router tears the stream
    down on the client's FIN/RST via its OWN disconnect watcher, not by
    a heartbeat write happening to fail."""
    backend, runner = await _make_streaming_backend(
        router_module, router_env, systemctl_stub, heartbeat_sec=30.0,
    )
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


# ── R14-F2: operator dashboard SSE (/api/<project>/events) cap ──────
#
# The streaming concurrency cap was gated ONLY on ``/mcp`` +
# ``/api/delivery/stream``. The operator dashboard SSE
# ``/api/<project>/events`` (→ backend ``/api/events``) was NOT in that
# set, so it was ALWAYS admitted: it held no cap slot at all. Any
# operator (viewer tier included) could open unbounded concurrent
# event streams. These tests pin the fix — ``/api/events`` gets the same
# admission control + slot accounting as ``/mcp`` — and guard the happy
# path (a single stream still works and frees its slot on close).


async def _register_active_streaming_backend(
    name, router_module, router_env, systemctl_stub, register_project,
    heartbeat_sec=None,
):
    """Register ``name`` via ``register_project`` (grants the sentinel
    operator membership so the cookie-authed events path clears the
    membership gate), start an infinite-SSE backend on its UDS, and mark
    its unit active. Returns ``(backend, runner)``."""
    register_project(name)
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _StreamingBackend(heartbeat_sec=heartbeat_sec)
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    return backend, runner


async def test_operator_events_sse_capped(
    aiohttp_client, router_app, router_module, router_env,
    systemctl_stub, register_project, monkeypatch,
) -> None:
    """More than the concurrency cap of concurrent ``/api/<project>/events``
    operator streams → the over-limit one is rejected with a clean 429.

    Pre-fix ``/api/events`` is NOT in the router's ``is_stream_request``
    set, so it holds NO cap slot: every stream is admitted,
    ``_streaming_proxies_global`` stays 0, and the over-limit request
    returns 200 — this test fails (RED). The fix adds ``/api/events`` to
    the stream set so it gets the same slot-accounted admission as
    ``/mcp``.
    """
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 2)
    _backend, runner = await _register_active_streaming_backend(
        "proj", router_module, router_env, systemctl_stub, register_project,
    )
    # Auto-logged-in sentinel operator (cookie), member of proj.
    client = await aiohttp_client(router_app)
    held = []

    async def _open_events():
        r = await asyncio.wait_for(
            client.get(
                "/agent-mcp/api/proj/events",
                headers={"Accept": "text/event-stream"},
            ),
            timeout=4.0,
        )
        await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=4.0)
        return r

    try:
        held.append(await _open_events())
        held.append(await _open_events())
        # Both admitted and holding a concurrency slot like /mcp does.
        assert _po._streaming_proxies_global == 2, (
            "operator events streams must hold a concurrency slot"
        )
        # The third must be rejected cleanly, not admitted nor hang.
        rejected = await asyncio.wait_for(
            client.get(
                "/agent-mcp/api/proj/events",
                headers={"Accept": "text/event-stream"},
            ),
            timeout=4.0,
        )
        assert rejected.status == 429
        rejected.close()
        assert _po._streaming_proxies_global == 2
    finally:
        for r in held:
            r.close()
        await runner.cleanup()


async def test_operator_events_happy_path_frees_slot(
    aiohttp_client, router_app, router_module, router_env,
    systemctl_stub, register_project,
) -> None:
    """A single operator events stream works end-to-end AND frees its
    slot + ``active_conns`` on close, so the idle reaper still fires.
    Regression guard for the F2 fix (must not break the happy path)."""
    _po = router_module._po
    _backend, runner = await _register_active_streaming_backend(
        "proj", router_module, router_env, systemctl_stub, register_project,
    )
    client = await aiohttp_client(router_app)
    try:
        resp = await asyncio.wait_for(
            client.get(
                "/agent-mcp/api/proj/events",
                headers={"Accept": "text/event-stream"},
            ),
            timeout=4.0,
        )
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        frame = await asyncio.wait_for(
            resp.content.readuntil(b"\n\n"), timeout=4.0,
        )
        assert frame == b"data: frame-1\n\n"
        assert _po.active_conns["proj"] == 1

        # Client goes away → slot + active_conns return to 0 so the idle
        # reaper can stop the backend.
        resp.close()
        assert await _poll_until(lambda: _po.active_conns["proj"] == 0), (
            "active_conns did not return to 0 after the operator events "
            "stream closed — the idle reaper stays pinned"
        )
        assert await _poll_until(
            lambda: _po._streaming_proxies_global == 0
        ), "streaming slot leaked after the operator events stream closed"
    finally:
        await runner.cleanup()


# ── R9-F3: teardown on FIN/RST must NOT wait for the next write ──────
#
# The aiohttp *TestClient* used above does not surface ``resp.close()``
# as a real socket close to the server (it pools/keeps the connection),
# so ``request.transport.is_closing()`` never flips for it and the only
# observable teardown signal under TestClient is a *write* failing. That
# is exactly the write-bound path #480 relied on — useless for pinning
# "released WITHOUT a subsequent write". These tests therefore drive the
# router over a REAL loopback TCP socket with a raw client, where a
# genuine FIN/RST is delivered and an independent disconnect watcher can
# observe it between upstream writes.


async def _serve_router_on_tcp(router_app):
    """Run ``router_app`` on a real 127.0.0.1 TCP port. Returns
    ``(runner, port)``; caller must ``await runner.cleanup()``."""
    runner = web.AppRunner(router_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def _raw_sse_open(port, path="/agent-mcp/mcp/proj"):
    """Open a raw SSE request over loopback TCP and read up to the first
    ``data:`` frame. Returns ``(reader, writer)`` — the caller owns the
    writer and can ``write_eof()`` (FIN) or ``transport.abort()`` (RST)
    to model a real client disconnect."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer tok-1234\r\n"
        "Accept: text/event-stream\r\n"
        "\r\n".encode()
    )
    await writer.drain()
    buf = b""
    while b"data: frame-1" not in buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=4.0)
        if not chunk:
            raise AssertionError(f"stream closed before first frame: {buf!r}")
        buf += chunk
    assert b"200" in buf.split(b"\r\n", 1)[0], f"bad status line: {buf!r}"
    return reader, writer


async def test_client_disconnect_frees_slot_without_a_subsequent_write(
    router_app, quiet_streaming_backend, router_module,
) -> None:
    """R9-F3: a disconnected SSE client's concurrency slot must free on
    the TCP FIN/RST — NOT only when the next upstream write fails.

    The backend goes quiet after the two opening frames (a 30 s
    heartbeat ≈ no further writes for the whole test). With write-bound
    detection (#480) the router notices the disconnect only on the next
    write, so ``active_conns`` stays pinned at 1 for ~a heartbeat → this
    test times out at 3 s (RED). An independent disconnect watcher frees
    the slot within its own short poll interval, with the backend having
    emitted ZERO heartbeat writes (GREEN).
    """
    _po = router_module._po
    backend = quiet_streaming_backend
    runner, port = await _serve_router_on_tcp(router_app)
    try:
        _reader, writer = await _raw_sse_open(port)
        assert _po.active_conns["proj"] == 1, "stream should count as one conn"
        assert backend.heartbeats_written == 0, "backend must be quiet"

        # Client vanishes: half-close sends a FIN with no further bytes.
        writer.write_eof()

        freed = await _poll_until(
            lambda: _po.active_conns["proj"] == 0, timeout=3.0,
        )
        # The slot must be back well under one (30 s) heartbeat AND the
        # backend must not have written anything since the frames — i.e.
        # teardown was driven by the disconnect watcher, not a write.
        assert backend.heartbeats_written == 0, (
            "backend emitted a heartbeat — teardown may be write-bound, "
            "not disconnect-driven"
        )
        assert freed, (
            "slot not freed on FIN — teardown is write-bound (waits for the "
            "next heartbeat) instead of racing an independent disconnect "
            "watcher"
        )
        # NB: we do NOT assert on ``backend.handler_exited`` here. The
        # router closes the upstream UDS socket the instant the slot
        # frees (that decrement happens on the ``ClientSession`` context
        # exit), but this quiet backend is parked in a 30 s sleep and
        # only observes its now-closed upstream on its next write — a
        # backend-side limitation, not the router's. The fast-heartbeat
        # ``test_client_disconnect_tears_down_upstream`` covers the
        # handler-exit signal.
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await runner.cleanup()
