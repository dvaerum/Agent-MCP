"""BL-R7-2 / BL-R7-3 — delete lifecycle hygiene.

BL-R7-2: ``delete_project_handler`` stopped the unit via
``_app._systemctl("stop", …)`` directly instead of the orchestrator's
``stop()`` (which pops ``last_active``). So a deleted project lingered
in ``last_active`` / ``list_active()`` until the idle reaper, and
``_schedule_backend_warm``'s ``(name,"backend") in last_active`` dedup
would then skip warm-starts for a same-name RE-created project. Fix:
pop the per-name orchestrator lifecycle maps on delete.

BL-R7-3: that same ``_systemctl`` ran synchronously ON the event loop
while holding ``_ensure_lock`` (~15-150 ms, or up to the SC-R7-2
timeout on a D-Bus stall), unlike round-6's ``asyncio.to_thread`` fix
in ``_ensure``. Fix: run it off-loop.

RED against origin/main: the pops don't exist, and the stop is a
direct on-loop call.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time

import pytest

from tests.harness import assert_ran_off_event_loop

pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── BL-R7-2: delete pops the project out of last_active ─────────────


async def test_delete_pops_project_from_last_active(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After DELETE, the project must be gone from ``last_active`` (and
    thus from ``list_active()``), not left for the idle reaper.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("ephemeral")
    from agent_mcp.router import project_orchestrator as _po

    # Simulate a warmed/active backend: seed the per-name lifecycle maps
    # exactly as a successful _ensure would.
    _po.last_active[("ephemeral", "backend")] = time.time()
    _po.unit_start_times[("ephemeral", "backend")] = time.monotonic()
    _po.forwarding_hmac_keys["ephemeral"] = b"x" * 32
    _po.ensure_failures[("ephemeral", "backend")] = (time.monotonic(), "old")

    assert ("ephemeral", "backend") in router_module.last_active

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/ephemeral", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("ephemeral", "backend") not in router_module.last_active, (
        "delete must pop the project from last_active — otherwise it "
        "lingers in list_active() and blocks a re-created project's warm"
    )
    # Sibling lifecycle maps are purged too.
    assert ("ephemeral", "backend") not in _po.unit_start_times
    assert "ephemeral" not in _po.forwarding_hmac_keys
    assert ("ephemeral", "backend") not in _po.ensure_failures
    # And it's absent from the orchestrator's list_active() snapshot.
    names = {row["name"] for row in _po.ProjectOrchestrator(
        router_module._REGISTRY).list_active()}
    assert "ephemeral" not in names


async def test_delete_only_pops_the_target_project(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Deleting one project must NOT evict a sibling's lifecycle state."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("gone")
    register_project("stays")
    from agent_mcp.router import project_orchestrator as _po

    _po.last_active[("gone", "backend")] = time.time()
    _po.last_active[("stays", "backend")] = time.time()

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/gone", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("gone", "backend") not in router_module.last_active
    assert ("stays", "backend") in router_module.last_active


# ── BL-R7-3: the delete systemctl runs off the event loop ───────────


async def test_delete_systemctl_runs_off_event_loop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """A slow ``systemctl stop`` during delete MUST NOT block the event
    loop — it must run via ``asyncio.to_thread`` (mirrors round-6
    BL-R6-2b in ``_ensure``). We block the stubbed ``stop`` in a worker
    thread and assert a sibling coroutine observes the loop as free
    almost immediately.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("slow-del")

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

    from agent_mcp.router import project_orchestrator as _po
    monkeypatch.setattr(_po, "_systemctl", _blocking_systemctl)

    loop_free_at: list[float] = []

    async def _probe() -> None:
        while not started.is_set():
            await asyncio.sleep(0.005)
        loop_free_at.append(time.monotonic())

    client = await aiohttp_client(router_app)

    delete = asyncio.create_task(
        client.delete(
            "/agent-mcp/api/router/projects/slow-del", headers=_ACCEPT,
        )
    )
    probe = asyncio.create_task(_probe())

    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="delete systemctl stop",
    )

    resp = await delete
    assert resp.status == 200, await resp.text()
    await probe
