"""R4-F2: the SSE concurrency cap (R8-F2's ``MAX_STREAMS_PER_AGENT`` /
``MAX_STREAMS_GLOBAL``) is admission-gated purely on ``req.method ==
"GET"`` for ``/mcp`` (see ``is_stream_request`` in
``agent_mcp/router/app.py``). But a ``POST /mcp`` ``tools/call`` to
``wait_for_events`` can ALSO return a long-lived ``text/event-stream``
response (the ``_HEARTBEAT_NO_CAP`` branch in
``agent_mcp/core/client_hold_strategy.py`` — an unrecognized
``clientInfo.name`` gets an uncapped indefinite hold) — and that path
was NOT admission-checked at all, because the pre-check at
``is_stream_request`` only fires for GET. This is the modality real
agents use for their resting wake-loop connection, so
``MAX_STREAMS_GLOBAL`` was effectively unenforced for the common case.

These tests pin the fix: a POST whose upstream response turns out to
be ``text/event-stream`` must be admission-checked against the SAME
per-agent / global cap as the GET path, keyed by the same
``_sse_agent_key``, at the point the upstream Content-Type is known
(the ``is_streaming`` check already used to pick the streaming pump).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

pytestmark = pytest.mark.asyncio


class _PostStreamingBackend:
    """UDS-bound aiohttp app whose ``POST /mcp`` emits an unbounded
    ``text/event-stream`` — models the backend's ``wait_for_events``
    uncapped heartbeat-hold branch. ``GET`` also streams (for the
    regression test), any other verb gets a normal buffered response.
    """

    HEARTBEAT_SEC = 0.02

    def __init__(self) -> None:
        self.concurrent_streams = 0
        self.max_concurrent_streams = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        await req.read()
        if req.method not in ("GET", "POST"):
            return web.Response(body=b"OK")
        self.concurrent_streams += 1
        self.max_concurrent_streams = max(
            self.max_concurrent_streams, self.concurrent_streams,
        )
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(req)
        try:
            await resp.write(b"data: frame-1\n\n")
            await resp.write(b"data: frame-2\n\n")
            while True:
                await asyncio.sleep(self.HEARTBEAT_SEC)
                await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.concurrent_streams -= 1
        return resp


async def _start_backend_on_uds(
    backend: _PostStreamingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def post_streaming_backend(router_module, router_env, systemctl_stub):
    """Infinite-SSE-on-POST backend for project 'proj', pre-registered +
    active, with the agent-token cache seeded so a bearer clears the
    router edge without a real ``/api/tokens`` round-trip."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._agent_token_cache["proj"] = (9.9e18, {"tok-1234": "Admin"})
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _PostStreamingBackend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


async def _poll_until(pred, timeout: float = 3.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


async def test_concurrent_post_sse_capped_per_agent(
    aiohttp_client, router_app, post_streaming_backend, router_module,
    monkeypatch,
) -> None:
    """More than ``MAX_STREAMS_PER_AGENT`` concurrent POST ``/mcp``
    ``wait_for_events``-shaped streams for one bearer must be rejected
    with a clean 429 once the cap is exceeded — the SAME behaviour the
    GET path already has.

    Pre-fix, ``is_stream_request`` is False for POST so the admission
    check never runs: every one of these is admitted (200), the live
    count is never tracked, and this test fails (RED). The fix
    retroactively acquires the same ``_track_streaming_proxy`` slot the
    instant the upstream Content-Type confirms streaming.
    """
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 2)
    client = await aiohttp_client(router_app)

    held = []

    async def _open_post_stream():
        r = await asyncio.wait_for(
            client.post(
                "/agent-mcp/mcp/proj",
                headers={
                    "Authorization": "Bearer tok-1234",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait_for_events"},
                },
            ),
            timeout=4.0,
        )
        await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=4.0)
        return r

    try:
        held.append(await _open_post_stream())
        held.append(await _open_post_stream())
        assert await _poll_until(
            lambda: _po._streaming_proxies_global == 2,
        ), "two admitted POST streams should hold two concurrency slots"

        rejected = await asyncio.wait_for(
            client.post(
                "/agent-mcp/mcp/proj",
                headers={
                    "Authorization": "Bearer tok-1234",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "wait_for_events"},
                },
            ),
            timeout=4.0,
        )
        assert rejected.status == 429, (
            f"expected 429 over the POST-stream cap, got {rejected.status} — "
            "the admission cap is not being enforced on the POST /mcp "
            "streaming path"
        )
        rejected.close()
        assert _po._streaming_proxies_global == 2, (
            "the rejected request must not have leaked a slot"
        )
    finally:
        for r in held:
            r.close()
        assert await _poll_until(
            lambda: _po._streaming_proxies_global == 0,
        ), "streaming slots leaked after held POST streams closed"


async def test_get_sse_cap_unaffected_by_post_cap_fix(
    aiohttp_client, router_app, post_streaming_backend, router_module,
    monkeypatch,
) -> None:
    """Regression guard: the pre-existing GET ``/mcp`` cap behaviour
    (admission-checked BEFORE the upstream connect) must be unaffected
    by adding the retroactive POST cap check."""
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 2)
    client = await aiohttp_client(router_app)

    held = []

    async def _open_get_stream():
        r = await asyncio.wait_for(
            client.get(
                "/agent-mcp/mcp/proj",
                headers={"Authorization": "Bearer tok-1234"},
            ),
            timeout=4.0,
        )
        await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=4.0)
        return r

    try:
        held.append(await _open_get_stream())
        held.append(await _open_get_stream())
        assert _po._streaming_proxies_global == 2

        rejected = await asyncio.wait_for(
            client.get(
                "/agent-mcp/mcp/proj",
                headers={"Authorization": "Bearer tok-1234"},
            ),
            timeout=4.0,
        )
        assert rejected.status == 429
        rejected.close()
        assert _po._streaming_proxies_global == 2
    finally:
        for r in held:
            r.close()


async def test_post_stream_happy_path_end_to_end(
    aiohttp_client, router_app, post_streaming_backend, router_module,
) -> None:
    """A normal, single POST ``/mcp`` ``wait_for_events``-shaped call
    still streams end-to-end and frees its slot on close — regression
    guard for the F2 fix (must not break the happy path)."""
    _po = router_module._po
    client = await aiohttp_client(router_app)

    resp = await asyncio.wait_for(
        client.post(
            "/agent-mcp/mcp/proj",
            headers={
                "Authorization": "Bearer tok-1234",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "wait_for_events"},
            },
        ),
        timeout=4.0,
    )
    try:
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"
        frame = await asyncio.wait_for(
            resp.content.readuntil(b"\n\n"), timeout=4.0,
        )
        assert frame == b"data: frame-1\n\n"
        assert await _poll_until(
            lambda: _po._streaming_proxies_global == 1,
        ), "the admitted POST stream should hold exactly one slot"
    finally:
        resp.close()
        assert await _poll_until(
            lambda: _po._streaming_proxies_global == 0,
        ), "streaming slot leaked after the POST stream closed"
