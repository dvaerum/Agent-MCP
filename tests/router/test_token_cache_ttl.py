"""Per-project agent-token cache holds its 3-second TTL.

The MCP messages handler hits ``_agent_token_map`` on every POST so
the cache lookup is on the hot path. A naive uncached implementation
would open a UDS connection to ``/api/tokens`` per request and
multiply backend load by N. We pin both arms:

  1. Within the TTL window, cached values are returned even after the
     underlying backend response would change.
  2. Past the TTL window, the next lookup fetches fresh.

retire-system-token Wave 2 (2026-06-23): the system_token /
``"Admin"`` pseudo-entry was dropped from the map. The cache now
holds only per-agent worker/manager tokens — the cookie-auth path
went off to ``_forwarding_header_from_cookie`` and no longer
consults this cache at all. The two tests below were rewritten to
mutate the agent-tokens response (rather than the
system_token file the router no longer reads) to drive cache
invalidation behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


class _TokenServer:
    """Stand-in for the backend's ``/api/tokens`` endpoint.

    Holds the agent-token list as a mutable attribute so a test can
    rotate it between calls and observe what the cache returns. After
    retire-system-token Wave 2 this is the ONLY input the cache
    layer consumes.
    """

    def __init__(self) -> None:
        self.agent_tokens: list[dict] = []
        self.call_count = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/tokens", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.Response:
        self.call_count += 1
        return web.json_response(
            {
                "agent_tokens": list(self.agent_tokens),
            },
        )


async def _bind(app: web.Application, sock_path: Path) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def token_backend(router_module, router_env, systemctl_stub):
    """Bind a token-serving backend at the project's UDS path."""
    name = "p"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    server = _TokenServer()
    server.agent_tokens = [{"agent_id": "worker-1", "token": "tok-v1"}]
    runner = await _bind(server.app(), sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield name, server
    finally:
        await runner.cleanup()


async def test_cache_holds_within_ttl_window(
    router_module, token_backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a controllable clock; assert the cached mapping is
    returned even after the underlying backend response would change.

    The router reads time via ``time.time()`` in the ``router``
    module, so we monkeypatch *that* reference rather than the stdlib
    module-wide.
    """
    name, server = token_backend

    clock = [1_000_000.0]
    monkeypatch.setattr(
        router_module.time, "time", lambda: clock[0],
    )

    first = await router_module._agent_token_map(name)
    assert "tok-v1" in first
    assert first["tok-v1"] == "worker-1"
    assert server.call_count == 1

    # Mutate what the backend would return — but stay within the TTL
    # window so the cache should short-circuit.
    server.agent_tokens = [{"agent_id": "worker-1", "token": "tok-v2"}]
    clock[0] += 2.0  # well under the 3-second TTL

    second = await router_module._agent_token_map(name)
    assert second == first, (
        "cache stale-within-TTL: returned a fresh mapping when the "
        "cached one was still valid"
    )
    assert server.call_count == 1, (
        f"backend was hit {server.call_count} times within TTL — "
        "the cache short-circuit isn't firing"
    )


async def test_cache_refreshes_after_ttl_expiry(
    router_module, token_backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the TTL window, the next call hits the backend again and
    picks up the new value."""
    name, server = token_backend

    clock = [2_000_000.0]
    monkeypatch.setattr(
        router_module.time, "time", lambda: clock[0],
    )

    first = await router_module._agent_token_map(name)
    assert "tok-v1" in first
    assert server.call_count == 1

    # Wind past the TTL (router uses _token_cache_ttl_sec = 3.0).
    server.agent_tokens = [{"agent_id": "worker-1", "token": "tok-v2"}]
    clock[0] += router_module._token_cache_ttl_sec + 0.1

    second = await router_module._agent_token_map(name)
    assert "tok-v2" in second, (
        "cache did not refresh past TTL — clients will see stale tokens"
    )
    assert "tok-v1" not in second
    assert server.call_count == 2, (
        f"expected 2 backend calls (initial + post-TTL refresh), "
        f"got {server.call_count}"
    )
