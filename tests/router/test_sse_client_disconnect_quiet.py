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
  4. The downstream write fails while the transport has NOT yet been
     flagged closing — the disconnect-handling code itself must survive
     that, see ``test_write_site_absorbs_reset_with_transport_not_closing``.

Each asserts two things: nothing was logged at ERROR by any logger an
operator watches — ``aiohttp.server`` OR the router's own outermost
middleware (the noise) — and the upstream was released so
``active_conns`` returns to 0 (no leaked backend connection pinning it
against the idle reaper).

The counter-property — a ``ConnectionError`` that is NOT a downstream
client-write failure must STAY loud — is pinned by
``test_backend_leg_connection_error_is_still_a_loud_error``. Without it
this file's quiet assertions could be satisfied by simply swallowing
every ``ConnectionError``, which would hide real backend faults.

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
from unittest import mock

import pytest
import pytest_asyncio
from aiohttp import ClientConnectionResetError, web
from aiohttp.test_utils import make_mocked_coro, make_mocked_request

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


# Every logger that renders a request failure as an operator-visible
# ERROR + traceback. ``aiohttp.server`` is what
# ``RequestHandler.handle_error`` uses when an exception escapes a web
# handler; ``agent_mcp.router.security_headers`` is our OUTERMOST
# middleware, which catches what would otherwise reach aiohttp and logs
# it itself. Watching only the former is how the live 5.74.0 regression
# hid in a green suite: the exception no longer escaped to aiohttp, it
# just produced the identical ERROR traceback one frame earlier. For the
# operator the two are the same noise, so both are asserted quiet.
_ERROR_LOGGERS = ("aiohttp.server", "agent_mcp.router.security_headers")


def _server_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every ERROR-or-worse record from any logger in ``_ERROR_LOGGERS``."""
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.name in _ERROR_LOGGERS and r.levelno >= logging.ERROR
    ]


_NO_ERROR = (
    "a peer disconnect was logged as an ERROR with a traceback; client "
    "disconnects are normal stream termination and must not look like "
    "application failures: {}"
)


async def _assert_server_stayed_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Assert nothing was logged at ERROR — after settling.

    The request task releases ``active_conns`` while UNWINDING, so the
    ERROR (logged once the exception reaches the middleware/aiohttp)
    lands strictly after any observable counter change. Poll for an error
    to appear — returning the instant one does — so the check neither
    races the log nor pays a fixed sleep on the passing path.
    """
    await _poll_until(lambda: bool(_server_errors(caplog)), timeout=2.0)
    errors = _server_errors(caplog)
    assert not errors, _NO_ERROR.format(errors)


@pytest.fixture
def capture_aiohttp_server_log(caplog: pytest.LogCaptureFixture):
    """Capture records at DEBUG from every logger in ``_ERROR_LOGGERS``."""
    with contextlib.ExitStack() as stack:
        for name in _ERROR_LOGGERS:
            stack.enter_context(caplog.at_level(logging.DEBUG, logger=name))
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


async def test_write_site_absorbs_reset_with_transport_not_closing(
    router_module, capture_aiohttp_server_log,
) -> None:
    """The downstream write fails, but ``request.transport`` does NOT yet
    report closing — the "is the peer gone?" question answered two ways.

    ``http_writer._write`` raises ``ClientConnectionResetError`` off the
    same ``protocol.transport`` that ``client_is_gone`` reads, so in the
    real server the two agree. This test forces them APART (a transport
    whose ``is_closing()`` is False, whose header write still raises) to
    pin the design decision: the disconnect is handled AT THE WRITE SITE,
    where a failed write to the client is itself proof the client is
    gone, and never depends on a second, separately-sampled signal.

    It is also the tightest reproduction of the live 5.74.0 regression:
    the write-site handler DID catch the reset, then raised ``NameError:
    name 'logger' is not defined`` out of its own logging call (the
    module's logger is ``log``), which the outermost middleware reported
    as an "Unhandled exception" ERROR + traceback. The peer-disconnect
    noise had moved one frame, not gone.
    """
    transport = mock.Mock()
    # The race, made explicit: the peer is going away, but nothing has
    # flagged the transport yet.
    transport.is_closing.return_value = False
    writer = mock.Mock()
    writer.write_headers = make_mocked_coro(None)
    writer.write = make_mocked_coro(None)
    writer.write_eof = make_mocked_coro(None)
    writer.drain = make_mocked_coro(None)
    writer.transport = transport
    writer.send_headers = mock.Mock(
        side_effect=ClientConnectionResetError(
            "Cannot write to closing transport",
        ),
    )
    req = make_mocked_request(
        "GET", "/agent-mcp/mcp/proj", transport=transport, writer=writer,
    )
    from agent_mcp.router.client_disconnect import client_is_gone

    assert client_is_gone(req) is False, (
        "the fixture must simulate the write failing while the transport "
        "still reports itself live — otherwise it does not test the seam"
    )

    resp = await router_module._stream_upstream_to_client(
        req, mock.Mock(status=200), "proj", {"Content-Type": "text/event-stream"},
    )

    assert isinstance(resp, web.StreamResponse)
    assert writer.send_headers.called, "the downstream header write never ran"
    await _assert_server_stayed_quiet(capture_aiohttp_server_log)


async def test_backend_leg_connection_error_is_still_a_loud_error(
    aiohttp_client_cls, capture_aiohttp_server_log,
) -> None:
    """The counter-property. A ``ConnectionError`` raised while the CLIENT
    is still connected is somebody else's reset — the backend leg, an
    upstream socket — and must keep its ERROR + traceback.

    This is what stops the quiet-disconnect handling from degrading into
    a blanket ``except ConnectionError``: the quiet path lives at the
    downstream WRITE site, where "the write to this client failed" is
    unambiguous. Anything that reaches the outermost middleware with a
    live client transport is a real fault and stays loud (and answers a
    generic 500, not the client-gone status).
    """
    from aiohttp.test_utils import TestServer

    from agent_mcp.router.security_headers import security_headers_middleware

    async def backend_leg_reset(request: web.Request) -> web.Response:
        # Shape of a router→backend UDS reset: raised while the
        # downstream peer is alive and waiting for its answer.
        raise ConnectionResetError("Connection lost")

    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_get("/backend-reset", backend_leg_reset)

    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        client = aiohttp_client_cls(server)
        await client.start_server()
        try:
            resp = await client.get("/backend-reset")
            assert resp.status == 500, (
                "an upstream fault must not be answered with the "
                "client-gone status — the client is still there"
            )
        finally:
            await client.close()
    finally:
        await server.close()

    errors = _server_errors(capture_aiohttp_server_log)
    assert errors, (
        "a ConnectionError raised while the client was still connected was "
        "logged quietly — that silently hides genuine backend faults, the "
        "exact regression the quiet-disconnect handling must not cause"
    )


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
