"""OBS-R34-RENAME-ONLOOP — rename systemctl-stop must run off the loop.

``rename_project_handler`` stopped the backend unit via
``_app._systemctl("stop", …)`` SYNCHRONOUSLY on the event loop.
``_systemctl`` is a blocking ``subprocess.run`` (~15-150 ms, or up to
the SC-R7-2 timeout on a D-Bus stall), so the whole aiohttp event loop —
every other concurrent router request — stalled for the duration of the
stop.

``delete_project_handler`` already fixed exactly this in BL-R7-3
(``await asyncio.to_thread(_app._systemctl, "stop", …)``); rename was the
missed sibling. This test mirrors
``test_delete_systemctl_runs_off_event_loop``: it blocks the stubbed
``stop`` in a worker thread and asserts a sibling coroutine observes the
loop as free almost immediately.

RED against the pre-fix code: rename's stop is a direct on-loop call, so
the probe can't run until the ~0.4 s blocking stop returns.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time

import pytest

from tests.harness import assert_ran_off_event_loop


pytestmark = [pytest.mark.asyncio]


_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def test_rename_systemctl_runs_off_event_loop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """A slow ``systemctl stop`` during rename MUST NOT block the event
    loop — it must run via ``asyncio.to_thread`` (mirrors delete's
    BL-R7-3). We block the stubbed ``stop`` in a worker thread and assert
    a sibling coroutine observes the loop as free almost immediately.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("slow-rename")

    started = threading.Event()
    started_at: list[float] = []
    BLOCK_SEC = 0.4

    def _blocking_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "stop":
            started_at.append(time.monotonic())
            started.set()
            time.sleep(BLOCK_SEC)  # bounded: no permanent hang on regress
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

    rename = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/slow-rename",
            data=json.dumps({"name": "renamed-fast", "grace_days": 7}),
            headers=_ACCEPT,
        )
    )
    probe = asyncio.create_task(_probe())

    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="rename systemctl stop",
    )

    resp = await rename
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["renamed"]["from"] == "slow-rename"
    assert body["renamed"]["to"] == "renamed-fast"
    await probe


async def test_stop_project_systemctl_runs_off_event_loop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """OBS-R34 class-sweep: ``stop_project_handler`` ran ``_is_active`` +
    ``_systemctl("stop", …)`` SYNCHRONOUSLY on the event loop. Both shell
    out (blocking ``subprocess.run``), so a slow stop stalled every other
    concurrent router request. They must run off-loop via
    ``asyncio.to_thread`` (mirrors delete's BL-R7-3 / orchestrator
    BL-R6-2b).
    """
    register_project("slow-stop")

    started = threading.Event()
    started_at: list[float] = []
    BLOCK_SEC = 0.4

    def _blocking_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        # ``stop_project_handler`` first probes ``is-active``; mark the
        # unit active so it proceeds to the (blocking) ``stop``.
        if verb == "is-active":
            return subprocess.CompletedProcess(list(args), 0, "", "")
        if verb == "stop":
            started_at.append(time.monotonic())
            started.set()
            time.sleep(BLOCK_SEC)  # bounded: no permanent hang on regress
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _blocking_systemctl)
    monkeypatch.setattr(router_module, "_is_active", lambda unit: True)
    from agent_mcp.router import project_orchestrator as _po
    monkeypatch.setattr(_po, "_systemctl", _blocking_systemctl)

    loop_free_at: list[float] = []

    async def _probe() -> None:
        while not started.is_set():
            await asyncio.sleep(0.005)
        loop_free_at.append(time.monotonic())

    client = await aiohttp_client(router_app)

    stop = asyncio.create_task(
        client.post(
            "/agent-mcp/api/router/projects/slow-stop/stop",
            data="{}",
            headers=_ACCEPT,
        )
    )
    probe = asyncio.create_task(_probe())

    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="stop-project systemctl stop",
    )

    resp = await stop
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["stopped"] == "slow-stop"
    await probe
