"""The idle reaper stops backends that have been quiet for ``IDLE_SEC``.

The reaper is an infinite loop in ``router.reaper`` that sleeps 60 s
between scans of ``last_active``. We can't wait for the real sleep
in a unit test, so we patch ``asyncio.sleep`` to a fast no-op and let
the loop tick once before we cancel it. Time itself is also mocked
so we can wind the clock forward / back deterministically.

Two cases pinned:

  1. A project last-active longer than ``IDLE_SEC`` ago gets a
     ``systemctl stop`` and is dropped from ``last_active``.
  2. A project last-active within the window does NOT get stopped.
"""

from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.asyncio


async def _one_reaper_tick(router_module, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the reaper loop until it has executed exactly one scan.

    The reaper looks like ``while True: await asyncio.sleep(60); ...``.
    We capture the real ``asyncio.sleep`` BEFORE monkeypatching so the
    fake can call into it; otherwise replacing ``router_module.asyncio.sleep``
    (which IS the live stdlib ``asyncio`` module's attribute) would
    cause the fake to recurse into itself.

    Strategy: first call → yield control once (so the test task can
    run the body); second call → suspend forever, so the loop body
    runs exactly once. We then cancel the task.
    """
    real_sleep = asyncio.sleep
    call_count = [0]
    forever = asyncio.Event()  # never set

    async def fake_sleep(_secs):
        call_count[0] += 1
        if call_count[0] >= 2:
            await forever.wait()
        await real_sleep(0)

    monkeypatch.setattr(router_module.asyncio, "sleep", fake_sleep)
    try:
        task = asyncio.create_task(router_module.reaper(None))
        # Give the loop one scheduling cycle to: enter, hit sleep,
        # advance, run the scan body, hit sleep again, suspend.
        for _ in range(5):
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        # monkeypatch's teardown restores asyncio.sleep; nothing more
        # to do here. The ``forever`` event is GC'd with the function
        # frame.
        pass


async def test_idle_project_is_stopped(
    router_module, router_env, systemctl_stub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "stale"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    # Plant a last_active timestamp older than IDLE_SEC by patching
    # time.time so router.reaper sees `now - last_active > IDLE_SEC`.
    last = 1_000_000.0
    router_module.last_active[(name, "backend")] = last
    monkeypatch.setattr(
        router_module.time, "time",
        lambda: last + router_module.IDLE_SEC + 60.0,
    )

    await _one_reaper_tick(router_module, monkeypatch)

    assert systemctl_stub.counts[("stop", unit)] == 1, (
        f"reaper did not stop idle project {name!r} — "
        f"systemctl call log: {systemctl_stub.calls}"
    )
    assert (name, "backend") not in router_module.last_active


async def test_recently_active_project_is_not_stopped(
    router_module, router_env, systemctl_stub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "fresh"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    last = 2_000_000.0
    router_module.last_active[(name, "backend")] = last
    # Wind forward less than IDLE_SEC — still inside the keep-alive
    # window.
    monkeypatch.setattr(
        router_module.time, "time",
        lambda: last + (router_module.IDLE_SEC // 2),
    )

    await _one_reaper_tick(router_module, monkeypatch)

    assert systemctl_stub.counts[("stop", unit)] == 0, (
        "reaper stopped a project that was last-active well inside "
        "the IDLE_SEC keep-alive window"
    )
    assert (name, "backend") in router_module.last_active
