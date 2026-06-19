"""Recent-failure cooldown in ``_ensure`` caps the cascade fan-out.

P005-followup (2026-06-19): the dashboard's first paint fans out 6
parallel reads (``/all-data``, ``/all-data``, ``/agents``, ``/tasks``,
``/tokens``, ``/context-data``). Without a cooldown, the router's
``_ensure_lock`` serialises them and each queued caller pays a fresh
20 s socket-wait behind a backend that's failing to come up. With the
client's 30 s AbortController firing first, every per-project fetch
aborts and the page renders empty.

The fix caches the most-recent ``_ensure`` failure per (name, role)
and re-raises the same 504 immediately for any caller that arrives
within ``ENSURE_FAILURE_COOLDOWN_SEC``. The first caller still pays
the full wait; the next N callers fail fast.

Regression-tested here:

  1. Six concurrent calls against a backend whose unit "starts" but
     never produces the UDS → first one waits, the others
     short-circuit on the cached failure. systemctl start is invoked
     exactly once.
  2. After the cooldown window elapses, a fresh call retries (and
     either succeeds or repeats the cycle).
  3. A successful ``_ensure`` evicts the failure entry so the next
     caller doesn't see a phantom cooldown for a now-healthy backend.
  4. A hard ``systemctl start`` failure (non-zero return) also feeds
     the cooldown — otherwise a stuck unit would loop on systemctl.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web


pytestmark = pytest.mark.asyncio


async def _start_uds_backend(sock_path: Path) -> web.AppRunner:
    """Bind a no-op aiohttp app to ``sock_path``."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


async def test_concurrent_failures_pay_socket_wait_once(
    router_module, router_env, systemctl_stub,
) -> None:
    """Six concurrent ``_ensure`` calls against a backend that never
    produces its socket → systemctl start invoked exactly once.

    Without the cooldown, every queued caller would enter
    ``_ensure``, see the unit "active" (the stub flips on start), see
    the socket missing, restart, and wait another budget — N × 20 s
    cascade. With the cooldown, the first caller pays the wait + 504s,
    the others see the cached failure and short-circuit.
    """
    name = "stuck-backend"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    # No UDS — the stub will mark the unit active on start but the
    # socket file never appears. AGENT_MCP_ENSURE_SOCKET_ATTEMPTS=1
    # in the test env caps the wait at ~0.1 s per attempt.
    router_module._clear_ensure_failures()
    results = await asyncio.gather(
        *[router_module._ensure(name, "backend") for _ in range(6)],
        return_exceptions=True,
    )
    # All six raise HTTPGatewayTimeout — the first from the real
    # socket-wait, the rest from the cooldown.
    assert all(isinstance(r, web.HTTPGatewayTimeout) for r in results), (
        f"expected six HTTPGatewayTimeouts, got types "
        f"{[type(r).__name__ for r in results]}"
    )
    unit = f"agent-mcp@{name}.service"
    start_count = (
        systemctl_stub.counts[("start", unit)]
        + systemctl_stub.counts[("restart", unit)]
    )
    assert start_count == 1, (
        f"expected exactly 1 systemctl start/restart, got {start_count} "
        "— the failure cooldown should suppress retries while the "
        "cached failure is still hot"
    )


async def test_cooldown_expires_and_allows_retry(
    router_module, router_env, systemctl_stub, monkeypatch,
) -> None:
    """Once the cooldown window elapses, the next call retries instead
    of returning the cached 504. This keeps a transient failure from
    becoming a permanent block.
    """
    name = "retry-after-cooldown"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    # 0 s cooldown → cache entry is effectively never honoured;
    # the second call must re-attempt and trigger another systemctl.
    monkeypatch.setattr(
        router_module._po, "ENSURE_FAILURE_COOLDOWN_SEC", 0.0,
    )
    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")
    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")
    unit = f"agent-mcp@{name}.service"
    total_starts = (
        systemctl_stub.counts[("start", unit)]
        + systemctl_stub.counts[("restart", unit)]
    )
    assert total_starts == 2, (
        f"expected two systemctl invocations after cooldown elapsed, "
        f"got {total_starts}"
    )


async def test_success_evicts_cached_failure(
    router_module, router_env, systemctl_stub,
) -> None:
    """A successful ``_ensure`` clears any cached failure so the next
    caller doesn't see a phantom cooldown for a now-healthy backend.
    """
    name = "recovers"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    # Force a failure first.
    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")
    assert (name, "backend") in router_module.ensure_failures
    # Now bring up the real UDS so the next _ensure succeeds. We have
    # to flip the unit "inactive" first so _ensure decides to restart
    # (which is a no-op on the stub — the socket is what gates
    # success), but the launcher would normally do that for us.
    sock = router_env.sock_dir / name / "backend.sock"
    runner = await _start_uds_backend(sock)
    try:
        path = await router_module._ensure(name, "backend")
    finally:
        await runner.cleanup()
    assert path == sock
    assert (name, "backend") not in router_module.ensure_failures, (
        "successful _ensure must evict the cached failure"
    )


async def test_systemctl_start_failure_is_cached_too(
    router_module, router_env, systemctl_stub,
) -> None:
    """A hard ``systemctl start`` non-zero return also feeds the
    cooldown. Otherwise a queued cascade would loop on systemctl
    against a unit-file / permission / OOM condition the next call
    has no chance of fixing in ~5 s.
    """
    name = "systemctl-broken"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    # Make systemctl fail every verb. The stub's `failures` dict keys
    # on verb name; setting both "start" and "restart" covers either
    # action _ensure picks.
    systemctl_stub.failures["start"] = 1
    systemctl_stub.failures["restart"] = 1
    with pytest.raises(web.HTTPInternalServerError):
        await router_module._ensure(name, "backend")
    # The next call short-circuits — same 504, no second systemctl.
    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")
    unit = f"agent-mcp@{name}.service"
    total_invocations = (
        systemctl_stub.counts[("start", unit)]
        + systemctl_stub.counts[("restart", unit)]
    )
    assert total_invocations == 1, (
        f"expected exactly 1 systemctl start/restart even after the "
        f"second _ensure, got {total_invocations}"
    )
