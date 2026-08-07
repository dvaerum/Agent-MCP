"""Graceful-shutdown semantics for an in-flight proxy connection.

Regression for the symptom seen in production at 08:57:17 CEST
2026-06-04: every ``systemctl --user restart agent-mcp-router.service``
during a deploy stalled in ``stop-sigterm`` for 90 s before systemd
SIGKILLed the router (TimeoutStopSec). Cause:
``_proxy_to_backend`` opens an aiohttp ClientSession to the
per-project backend UDS and ``await up.read()``s the whole body.
The MCP Streamable-HTTP dashboard channel keeps that response open
for minutes, and aiohttp's runner waits up to ``shutdown_timeout``
(default 60 s) for in-flight handlers to finish on cleanup. Without
``handler_cancellation`` or an explicit ``on_shutdown`` drain, the
handler never gets nudged — so the router blocks until SIGKILL.

The fix has three parts:

1. Track every ``_proxy_to_backend`` task on the app
   (``app["_proxy_to_backend_tasks"]``).
2. An ``on_shutdown`` hook cancels each tracked task.
3. ``web.run_app`` is given ``shutdown_timeout=3.0`` so even if a
   task wedges on cancel-cleanup the runner returns inside the
   systemd ``TimeoutStopSec`` window.

This test exercises (1) + (2): it stands up the router app with a
real UDS backend that *never finishes its response*, opens a
streaming client request, then calls ``runner.cleanup()`` (the same
entry point ``web.run_app`` invokes on SIGTERM) and asserts the
cleanup completes within 5 s. Against unpatched main this hangs
for ~60 s (the aiohttp default ``shutdown_timeout``), failing the
``asyncio.wait_for`` assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

pytestmark = pytest.mark.asyncio


class _HangingBackend:
    """UDS backend that opens an SSE-shaped response and never finishes.

    Mirrors the real per-project agent-mcp backend's MCP /mcp endpoint
    under Streamable-HTTP: a ``StreamResponse`` is prepared, one chunk
    is written so the client sees the response start, then the
    coroutine awaits forever. ``_proxy_to_backend`` is stuck on
    ``await up.read()`` for the duration.
    """

    def __init__(self) -> None:
        self.started: asyncio.Event = asyncio.Event()
        self._release: asyncio.Event = asyncio.Event()

    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_route("*", "/{tail:.*}", self._handle)
        return a

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(req)
        # Send one chunk so the client returns from ``response.read()``-
        # equivalent first-chunk read and the proxy is now blocked on
        # the next read. The router's _proxy_to_backend does
        # ``body = await up.read()`` which never completes here.
        await resp.write(b"data: started\n\n")
        self.started.set()
        # Wait forever (until test tears down the backend runner).
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            pass
        return resp


async def _start_backend_on_uds(
    backend: _HangingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def hanging_backend(router_module, router_env, systemctl_stub):
    """Stand up a UDS backend that never finishes its response."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _HangingBackend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        # Wake the backend handler so its runner.cleanup() can return
        # promptly — otherwise the SSE-style handler awaits forever
        # and pytest teardown sits on aiohttp's shutdown_timeout.
        backend._release.set()
        await runner.cleanup()


async def test_runner_cleanup_completes_promptly_with_inflight_proxy(
    router_app, router_module, hanging_backend, unused_tcp_port,
) -> None:
    """``AppRunner.cleanup()`` must return inside 5 s even while a
    proxied client request is mid-stream.

    The router was previously inheriting aiohttp's 60 s default
    ``shutdown_timeout`` and had no ``on_shutdown`` drain, so the
    cleanup blocked until SIGKILL.
    """
    # Pre-seed the token cache so the auth pre-check passes without
    # opening a real /api/tokens connection.
    router_module._agent_token_cache["proj"] = (
        9.9e18, {"tok-1234": "Admin"},
    )

    runner = web.AppRunner(router_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()

    # Start a streaming client request in the background; do NOT
    # await its body — the backend will hold it open forever.
    client_session = aiohttp.ClientSession()

    async def _open_stream() -> None:
        with contextlib.suppress(Exception):
            async with client_session.post(
                f"http://127.0.0.1:{unused_tcp_port}/agent-mcp/mcp/proj",
                data=b"{}",
                headers={"Authorization": "Bearer tok-1234"},
            ) as resp:
                # Read until the proxy or the cleanup tears it down.
                with contextlib.suppress(Exception):
                    async for _ in resp.content.iter_chunked(1):
                        pass

    client_task = asyncio.create_task(_open_stream())
    try:
        # Wait until the backend has actually started responding so
        # we know _proxy_to_backend is blocked on ``up.read()``.
        await asyncio.wait_for(hanging_backend.started.wait(), timeout=5.0)

        # The system under test: cleanup must complete inside 5 s.
        # On unpatched main this blocks for ~60 s (aiohttp's default
        # ``shutdown_timeout``) and the wait_for raises TimeoutError.
        await asyncio.wait_for(runner.cleanup(), timeout=5.0)
    finally:
        client_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await client_task
        await client_session.close()
