"""R5-F7 [MEDIUM, availability/DoS-guard]: verify R4-F2's retroactive
POST-stream admission cap (``MAX_STREAMS_PER_AGENT``) holds under a
REAL simultaneous burst from a single agent bearer — not just the
sequential opens ``test_sse_proxy_post_cap.py`` already covers.

Background: a round-5 pentest reported 6 live trials of 10 truly
concurrent ``POST /mcp`` ``wait_for_events`` calls (same bearer)
against the deployed router, observing 5/7/6/5/7/5 simultaneously-
admitted streams — never <= 4 — despite R4-F2 (PR #661) having already
landed. Two race candidates were flagged for investigation: (1) a
genuine check-and-increment race in the retroactive cap acquisition in
``app.py``, or (2) the plain module-level ``int``/``dict`` counters in
``project_orchestrator.py`` (``_streaming_proxies_global`` /
``_streaming_proxies_per_agent``) not being protected against
concurrent read-then-write.

Investigation: extensive concurrent-burst testing against the CURRENT
code (which already carries the R4-F2 fix) — both in-process
``asyncio.gather`` bursts up to 60-wide, and genuine separate OS
``curl`` processes hitting a really-bound TCP socket, repeated across
many trials — never exceeded the cap. The check-then-increment in
``_track_streaming_proxy`` has no ``await`` between the read and the
mutation, so it was already atomic against asyncio's single-threaded
cooperative scheduler; the live pentest's over-admission most likely
observed a router build that predated PR #661 (a busy router serves
its old build until it idles or is explicitly restarted).

That said, the pre-fix atomicity was an INCIDENTAL property (no
`await` happens to sit between the check and the mutation) rather than
a structural guarantee — trivially broken by a future edit that adds
one. The fix wraps the check-and-increment (and the matching decrement)
in an ``asyncio.Lock`` scoped tightly around just that critical
section, making the invariant explicit and durable. These tests pin
the desired behaviour as a permanent regression guard: a real
concurrent burst — mirroring the live repro's 10-concurrent-calls
shape as closely as an in-process harness can — must never admit more
than ``MAX_STREAMS_PER_AGENT`` streams, and every rejected caller must
see a clean 429 without leaking a slot.
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
    uncapped heartbeat-hold branch."""

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
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(req)
        try:
            await resp.write(b"data: frame-1\n\n")
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


def _wait_for_events_request(client, id_: int):
    return client.post(
        "/agent-mcp/mcp/proj",
        headers={
            "Authorization": "Bearer tok-1234",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": id_,
            "method": "tools/call",
            "params": {"name": "wait_for_events"},
        },
    )


async def test_real_concurrent_burst_never_exceeds_per_agent_cap(
    aiohttp_client, router_app, post_streaming_backend, router_module,
    monkeypatch,
) -> None:
    """10 GENUINELY concurrent (``asyncio.gather``, not sequential-await)
    ``POST /mcp`` ``wait_for_events`` calls from the SAME bearer —
    mirroring the live repro's shape — must admit AT MOST
    ``MAX_STREAMS_PER_AGENT`` and reject the rest with a clean 429.
    """
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 4)
    client = await aiohttp_client(router_app)

    async def _fire(i: int):
        return await asyncio.wait_for(
            _wait_for_events_request(client, i), timeout=8.0,
        )

    results = await asyncio.gather(*[_fire(i) for i in range(10)])
    try:
        statuses = [r.status for r in results]
        admitted = sum(1 for s in statuses if s == 200)
        rejected = sum(1 for s in statuses if s == 429)
        assert admitted == 4, (
            f"expected exactly 4 admitted streams under the real "
            f"concurrent burst, got {admitted}: {statuses}"
        )
        assert admitted + rejected == 10, statuses
        assert _po._streaming_proxies_global == 4, (
            "the live counter must match the number of admitted streams "
            f"— got {_po._streaming_proxies_global}"
        )
    finally:
        for r in results:
            r.close()


async def test_real_concurrent_burst_rejects_without_leaking_a_slot(
    aiohttp_client, router_app, post_streaming_backend, router_module,
    monkeypatch,
) -> None:
    """Every request rejected by the real concurrent burst must NOT
    have incremented the live counter — only the admitted ones hold a
    slot, and closing them all must drain the counter to zero."""
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 4)
    client = await aiohttp_client(router_app)

    async def _fire(i: int):
        return await asyncio.wait_for(
            _wait_for_events_request(client, i), timeout=8.0,
        )

    results = await asyncio.gather(*[_fire(i) for i in range(10)])
    for r in results:
        r.close()

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline and _po._streaming_proxies_global != 0:
        await asyncio.sleep(0.02)
    assert _po._streaming_proxies_global == 0, (
        "streaming slots leaked after every held/rejected stream closed"
    )
    assert _po._streaming_proxies_per_agent == {}


async def test_real_concurrent_burst_at_higher_fan_out_still_holds(
    aiohttp_client, router_app, post_streaming_backend, router_module,
    monkeypatch,
) -> None:
    """A wider burst (30-way, still one bearer) stresses the
    check-and-increment harder than the live repro's 10-way shape —
    the cap must hold regardless of fan-out."""
    _po = router_module._po
    monkeypatch.setattr(_po, "MAX_STREAMS_PER_AGENT", 4)
    client = await aiohttp_client(router_app)

    async def _fire(i: int):
        return await asyncio.wait_for(
            _wait_for_events_request(client, i), timeout=8.0,
        )

    results = await asyncio.gather(*[_fire(i) for i in range(30)])
    try:
        admitted = sum(1 for r in results if r.status == 200)
        assert admitted == 4, (
            f"expected exactly 4 admitted streams at 30-way fan-out, "
            f"got {admitted}"
        )
    finally:
        for r in results:
            r.close()
