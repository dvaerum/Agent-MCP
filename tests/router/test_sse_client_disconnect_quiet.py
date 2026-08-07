"""A client disconnect on a proxied stream is NORMAL, not an ERROR.

Observed live on agent-mcp 5.74.0: an operator-dashboard SSE consumer
that opened and closed ``operator_events`` streams rapidly made the
router emit a full ``aiohttp.server`` ERROR traceback ending in::

    ClientConnectionResetError: Cannot write to closing transport

...raised from ``_stream_upstream_to_client``'s ``await resp.prepare(req)``
— i.e. the peer vanished in the window between the router opening the
upstream and writing the proxied response's headers downstream.

That is a peer going away, not an application fault, and logging it at
ERROR with a traceback is actively harmful: it is indistinguishable at a
glance from a genuine failure, so every occurrence costs a triage. These
tests pin the whole class at the seams a real peer disconnect can hit:

  1. Peer gone BEFORE the proxied response's headers are written
     (``resp.prepare``) — the reported bug.
  2. Peer gone MID-BODY, between frames of a live stream.
  3. Peer gone while the router is still reading the peer's REQUEST body
     (the same ``ConnectionResetError`` from the other direction).

Each asserts two things: nothing was logged at ERROR by ``aiohttp.server``
(the noise), and the upstream was released so ``active_conns`` returns to
0 (no leaked backend connection pinning it against the idle reaper).

Like the R9-F3 tests in ``test_sse_proxy_streaming.py``, these drive the
router over a REAL loopback TCP socket with a raw client: aiohttp's
``TestClient`` pools its connections, so ``resp.close()`` never delivers
a FIN/RST the server can observe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

pytestmark = pytest.mark.asyncio


# ── Backend stand-in ────────────────────────────────────────────────


class _GatedStreamingBackend:
    """UDS-bound aiohttp app modelling the backend's SSE endpoint, with a
    gate the test opens to control WHEN upstream response headers arrive.

    ``GET`` blocks on ``release`` before preparing its own response, so a
    test can park the router inside ``sess.request(...)`` — after the
    downstream request was accepted, before the router writes any
    downstream byte — and kill the peer in exactly that window. Once
    released it emits one frame then a fast ``: ping`` heartbeat forever,
    the shape of the real ``GET /mcp`` stream.

    Any other method answers immediately with a small buffered body (the
    non-streaming proxy path).
    """

    HEARTBEAT_SEC = 0.02

    def __init__(self) -> None:
        self.get_started = asyncio.Event()
        self.release = asyncio.Event()
        self.handler_exited = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        await req.read()
        if req.method != "GET":
            return web.Response(body=b"OK")
        self.get_started.set()
        await self.release.wait()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(req)
        try:
            await resp.write(b"data: frame-1\n\n")
            while True:
                await asyncio.sleep(self.HEARTBEAT_SEC)
                await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            # Router closed the upstream — the client went away.
            pass
        finally:
            self.handler_exited.set()
        return resp


async def _start_backend_on_uds(
    backend: _GatedStreamingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def gated_backend(router_module, router_env, systemctl_stub):
    """Project ``proj``, registered + active, its bearer cached, and a
    gated SSE backend listening on its UDS."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._agent_token_cache[name] = (9.9e18, {"tok-1234": "Admin"})
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    backend = _GatedStreamingBackend()
    runner = await _start_backend_on_uds(
        backend, router_env.sock_dir / name / "backend.sock",
    )
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── Harness ─────────────────────────────────────────────────────────


async def _serve_router_on_tcp(router_app):
    """Run ``router_app`` on a real 127.0.0.1 port; returns (runner, port)."""
    runner = web.AppRunner(router_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _poll_until(pred, timeout: float = 5.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def _server_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every ERROR-or-worse record aiohttp's server logger emitted.

    ``aiohttp.server`` is the logger ``RequestHandler.handle_error`` uses
    when an exception escapes a web handler — the exact "Error handling
    request from ..." + traceback seen in production.
    """
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "aiohttp.server" and r.levelno >= logging.ERROR
    ]


_NO_ERROR = (
    "a peer disconnect was logged as an aiohttp.server ERROR with a "
    "traceback; client disconnects are normal stream termination and "
    "must not look like application failures: {}"
)


async def _assert_server_stayed_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Assert ``aiohttp.server`` logged nothing at ERROR — after settling.

    The request task releases ``active_conns`` while UNWINDING, so the
    ERROR (logged once the exception reaches aiohttp) lands strictly
    after any observable counter change. Poll for an error to appear —
    returning the instant one does — so the check neither races the log
    nor pays a fixed sleep on the passing path.
    """
    await _poll_until(lambda: bool(_server_errors(caplog)), timeout=2.0)
    errors = _server_errors(caplog)
    assert not errors, _NO_ERROR.format(errors)


@pytest.fixture
def capture_aiohttp_server_log(caplog: pytest.LogCaptureFixture):
    """Capture ``aiohttp.server`` records at DEBUG for the test body."""
    with caplog.at_level(logging.DEBUG, logger="aiohttp.server"):
        yield caplog


async def _open_raw(port: int, request_bytes: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request_bytes)
    await writer.drain()
    return reader, writer


def _sse_request(path: str = "/agent-mcp/mcp/proj") -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Authorization: Bearer tok-1234\r\n"
        "Accept: text/event-stream\r\n"
        "\r\n"
    ).encode()


# ── Tests ───────────────────────────────────────────────────────────


async def test_disconnect_before_response_headers_is_not_an_error(
    router_app, gated_backend, router_module, capture_aiohttp_server_log,
) -> None:
    """The reported bug: the peer vanishes while the router is still
    waiting on upstream headers, so the router's very first downstream
    write (``resp.prepare``) hits a closing transport.

    Unfixed, ``ClientConnectionResetError`` escapes the handler, unwinds
    through every middleware, and aiohttp renders it as a 500 + ERROR
    traceback on ``aiohttp.server``. Fixed, it is recognised as the
    normal end of the stream: logged at DEBUG, upstream released.
    """
    _po = router_module._po
    runner, port = await _serve_router_on_tcp(router_app)
    try:
        _reader, writer = await _open_raw(port, _sse_request())
        assert await _poll_until(gated_backend.get_started.is_set), (
            "router never reached the backend"
        )
        # Peer vanishes in the pre-headers window.
        writer.transport.abort()
        assert await _poll_until(
            lambda: _po.active_conns["proj"] == 1
            and gated_backend.get_started.is_set(),
        )
        await asyncio.sleep(0.2)  # let the server observe the FIN/RST
        # Upstream headers only NOW arrive → the router tries its first
        # downstream write against a transport that is already closing.
        gated_backend.release.set()

        assert await _poll_until(lambda: _po.active_conns["proj"] == 0), (
            "upstream was not released after the peer disconnected — the "
            "orphaned connection pins the backend against the idle reaper"
        )
        await _assert_server_stayed_quiet(capture_aiohttp_server_log)
    finally:
        await runner.cleanup()


async def test_disconnect_mid_stream_is_not_an_error(
    router_app, gated_backend, router_module, capture_aiohttp_server_log,
) -> None:
    """The other half of the class: the peer leaves BETWEEN frames, so
    the failing downstream write is a body write, not the header write.
    """
    _po = router_module._po
    runner, port = await _serve_router_on_tcp(router_app)
    try:
        gated_backend.release.set()
        reader, writer = await _open_raw(port, _sse_request())
        buf = b""
        while b"data: frame-1" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert chunk, f"stream closed before the first frame: {buf!r}"
            buf += chunk
        assert _po.active_conns["proj"] == 1

        writer.transport.abort()

        assert await _poll_until(lambda: _po.active_conns["proj"] == 0), (
            "upstream was not released after a mid-stream peer disconnect"
        )
        await _assert_server_stayed_quiet(capture_aiohttp_server_log)
    finally:
        await runner.cleanup()


async def test_disconnect_while_reading_request_body_is_not_an_error(
    router_app, gated_backend, router_module, capture_aiohttp_server_log,
) -> None:
    """Same class from the other direction: the peer dies mid-upload, so
    the router's ``await req.read()`` raises ``ConnectionResetError``
    ("Connection lost") instead of a downstream write failing.
    """
    _po = router_module._po
    runner, port = await _serve_router_on_tcp(router_app)
    try:
        # Promise 200 bytes of body, send 10, then vanish.
        _reader, writer = await _open_raw(
            port,
            b"POST /agent-mcp/mcp/proj HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer tok-1234\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 200\r\n"
            b"\r\n"
            b'{"jsonrpc"',
        )
        await asyncio.sleep(0.2)
        writer.transport.abort()

        assert await _poll_until(lambda: _po.active_conns["proj"] == 0), (
            "the aborted upload left a connection counted against the project"
        )
        await _assert_server_stayed_quiet(capture_aiohttp_server_log)
    finally:
        with contextlib.suppress(Exception):
            await runner.cleanup()
