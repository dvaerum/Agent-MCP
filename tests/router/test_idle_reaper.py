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
import subprocess
import threading
import time

import pytest

from tests.harness import assert_ran_off_event_loop

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


# ── SEC-R34: the idle-reaper stop must run off the event loop ────────
#
# ``_reaper_tick`` ran ``_systemctl("stop", …)`` SYNCHRONOUSLY on the
# shared aiohttp event loop. ``_systemctl`` is a blocking
# ``subprocess.run`` (~15-150 ms, or up to the SC-R7-2 timeout on a
# D-Bus stall), so reaping a batch of idle units stalled EVERY other
# concurrent router request for the duration of each stop. This is the
# final sibling of the "blocking-systemctl-on-event-loop" class already
# fixed in request handlers (delete BL-R7-3, ensure/start BL-R6-2b,
# rename+stop OBS-R34, overview #387). It must run off-loop via
# ``asyncio.to_thread`` — mirroring ``_ensure``'s own off-loop pattern.


async def test_reaper_stop_runs_off_event_loop(
    router_module, router_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow ``systemctl stop`` inside a reaper tick MUST NOT block the
    event loop — it must run via ``asyncio.to_thread``. We block the
    stubbed ``stop`` in a worker thread and assert a sibling coroutine
    observes the loop as free almost immediately.

    RED against the pre-fix code: the reaper's stop is a direct on-loop
    call, so the probe can't run until the ~0.4 s blocking stop returns.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "slow-reap"

    last = 1_000_000.0
    _po.last_active[(name, "backend")] = last
    # Wind the clock past IDLE_SEC so this entry is decided idle.
    monkeypatch.setattr(
        _po.time, "time", lambda: last + _po.IDLE_SEC + 60.0,
    )

    started = threading.Event()
    started_at: list[float] = []
    BLOCK_SEC = 0.4

    def _blocking_systemctl(*args: str) -> subprocess.CompletedProcess:
        if args and args[0] == "stop":
            started_at.append(time.monotonic())
            started.set()
            time.sleep(BLOCK_SEC)  # bounded: no permanent hang on regress
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _blocking_systemctl)

    loop_free_at: list[float] = []

    async def _probe() -> None:
        while not started.is_set():
            await asyncio.sleep(0.005)
        loop_free_at.append(time.monotonic())

    tick = asyncio.create_task(_po._reaper_tick())
    probe = asyncio.create_task(_probe())

    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="reaper systemctl stop",
    )

    await tick
    await probe
    # The idle unit was still stopped + dropped from tracking.
    assert (name, "backend") not in _po.last_active


async def test_reaper_does_not_drop_unit_reactivated_during_stop(
    router_module, router_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU guard: awaiting the off-loop stop yields the event loop, so
    a concurrent ``_ensure`` warm-start can re-activate the backend and
    refresh its ``last_active`` timestamp WHILE the stop runs. The reaper
    must NOT drop that now-live backend from tracking — otherwise it
    silently falls out of the reaper's view and is never reaped again.

    RED against the pre-fix code: it pops ``last_active[key]``
    unconditionally after the stop, discarding the refreshed timestamp.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "reactivated"
    idle_ts = 1_000_000.0
    fresh_ts = 9_000_000.0
    key = (name, "backend")
    _po.last_active[key] = idle_ts
    _po.unit_start_times[key] = idle_ts

    # Decided idle at tick start.
    monkeypatch.setattr(
        _po.time, "time", lambda: idle_ts + _po.IDLE_SEC + 60.0,
    )

    def _reactivating_stop(*args: str) -> subprocess.CompletedProcess:
        if args and args[0] == "stop":
            # Simulate a concurrent _ensure re-activating the backend
            # and refreshing its timestamp DURING the stop.
            _po.last_active[key] = fresh_ts
            _po.unit_start_times[key] = fresh_ts
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _reactivating_stop)

    await _po._reaper_tick()

    # The refreshed (live) timestamp must survive — the reaper must not
    # drop a backend that came back to life during its stop.
    assert _po.last_active.get(key) == fresh_ts, (
        "reaper dropped a backend that was re-activated during its stop"
    )
    assert _po.unit_start_times.get(key) == fresh_ts
