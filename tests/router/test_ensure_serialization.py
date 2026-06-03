"""Per-(name, role) lock serialises ``_ensure()`` calls.

The dashboard's first-paint fires several parallel API calls
(``/status``, ``/agents``, ``/tasks``, ``/graph-data``). Without the
``ensure_locks`` mutex each one races ``systemctl`` independently —
fastest wins, the rest see the unit in a transient state and issue a
``restart``, causing a stop/start storm and a ~10-second window where
requests 504.

This test fires ``N=10`` parallel ``_ensure(name, role)`` calls via
``asyncio.gather`` and asserts that ``systemctl start`` runs exactly
once per (name, role).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web


pytestmark = pytest.mark.asyncio


async def _start_uds_backend(sock_path: Path) -> web.AppRunner:
    """Bind a no-op aiohttp app to ``sock_path`` so ``_ensure``'s
    ``sock.is_socket()`` check passes without us mocking it."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


async def test_concurrent_ensure_calls_invoke_systemctl_once(
    router_module, router_env, systemctl_stub,
) -> None:
    """10 concurrent ``_ensure(name, 'backend')`` calls → exactly one
    ``systemctl start agent-mcp@<name>.service``.

    The unit is NOT marked active up front, so the first lock-holder
    sees ``is-active = False`` and issues a start. The rest, when
    they finally acquire the lock, find ``is-active = True`` (the
    stub marked it on start) AND the socket file present, and skip
    the start entirely.
    """
    name = "stress"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    runner = await _start_uds_backend(sock)
    try:
        results = await asyncio.gather(
            *[router_module._ensure(name, "backend") for _ in range(10)],
        )
    finally:
        await runner.cleanup()

    assert all(p == sock for p in results)

    unit = f"agent-mcp@{name}.service"
    start_count = systemctl_stub.counts[("start", unit)]
    restart_count = systemctl_stub.counts[("restart", unit)]

    assert start_count == 1, (
        f"expected exactly 1 systemctl start, got {start_count} — "
        "ensure_locks mutex regressed; the dashboard's first-paint "
        "fan-out will cause a stop/start storm"
    )
    assert restart_count == 0, (
        f"expected zero restarts, got {restart_count} — the lock-holder "
        "should NEVER need to restart since the unit was inactive"
    )


async def test_ensure_serialises_per_name_not_globally(
    router_module, router_env, systemctl_stub,
) -> None:
    """Two different projects must each get their own start — the
    lock is per (name, role), not a global mutex. Otherwise spinning
    up project A blocks project B and the dashboard cross-project
    picker feels sluggish.
    """
    for n in ("alpha", "beta"):
        router_module._REGISTRY.register(n, str(router_env.root / f"ws-{n}"))
    sock_a = router_env.sock_dir / "alpha" / "backend.sock"
    sock_b = router_env.sock_dir / "beta" / "backend.sock"
    runner_a = await _start_uds_backend(sock_a)
    runner_b = await _start_uds_backend(sock_b)
    try:
        await asyncio.gather(
            router_module._ensure("alpha", "backend"),
            router_module._ensure("beta", "backend"),
        )
    finally:
        await runner_a.cleanup()
        await runner_b.cleanup()

    assert systemctl_stub.counts[("start", "agent-mcp@alpha.service")] == 1
    assert systemctl_stub.counts[("start", "agent-mcp@beta.service")] == 1
