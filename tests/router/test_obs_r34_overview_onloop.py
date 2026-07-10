"""OBS-R34-RENAME-ONLOOP class-sweep — overview probes must run off-loop.

``overview_handler`` built its envelope via
``_app._build_overview_envelope()`` SYNCHRONOUSLY on the event loop. That
builder loops over EVERY registered project calling ``_is_active(unit)``
— which is ``_systemctl("is-active", unit)``, a blocking
``subprocess.run`` — plus a blocking SQLite COUNT fan-out per project.
So a single ``GET /overview`` stalled the whole aiohttp event loop (every
other concurrent router request) for N sequential ``systemctl is-active``
subprocess calls (N = number of projects).

This is the last sibling of the class #386 swept (rename/stop). The whole
read-only builder must run off-loop via ``asyncio.to_thread`` (mirrors
delete's BL-R7-3 and the rename/stop fixes).

RED against the pre-fix code: the builder runs on-loop, so a slow
``is-active`` probe blocks the loop and the sibling probe coroutine can't
advance until it returns.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time

import pytest

from tests.harness import assert_ran_off_event_loop


pytestmark = [pytest.mark.asyncio]


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


async def test_overview_systemctl_probes_run_off_event_loop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch,
) -> None:
    """A slow ``systemctl is-active`` probe during the overview build MUST
    NOT block the event loop — the whole builder must run via
    ``asyncio.to_thread``. We block the stubbed ``is-active`` in a worker
    thread and assert a sibling coroutine observes the loop as free almost
    immediately.
    """
    register_project("slow-overview")
    # Fresh module => cache starts None, but be explicit so a warm cache
    # from any prior state can't short-circuit the build under test.
    router_module._overview_cache = None

    started = threading.Event()
    started_at: list[float] = []
    BLOCK_SEC = 0.4

    def _blocking_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "is-active":
            started_at.append(time.monotonic())
            started.set()
            time.sleep(BLOCK_SEC)  # bounded: no permanent hang on regress
            # rc=3 → not active → status "stopped" (unchanged semantics).
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _blocking_systemctl)
    from agent_mcp.router import project_orchestrator as _po
    monkeypatch.setattr(_po, "_systemctl", _blocking_systemctl)

    loop_free_at: list[float] = []

    async def _probe() -> None:
        while not started.is_set():
            await asyncio.sleep(0.005)
        loop_free_at.append(time.monotonic())

    client = await aiohttp_client(router_app)

    overview = asyncio.create_task(
        client.get("/agent-mcp/api/router/overview", headers=_STRICT_ACCEPT)
    )
    probe = asyncio.create_task(_probe())

    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="overview systemctl is-active probe",
    )

    resp = await overview
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    names = [p["name"] for p in body["projects"]]
    assert "slow-overview" in names
    row = next(p for p in body["projects"] if p["name"] == "slow-overview")
    assert row["status"] == "stopped"  # rc=3 => not active, unchanged
    await probe
