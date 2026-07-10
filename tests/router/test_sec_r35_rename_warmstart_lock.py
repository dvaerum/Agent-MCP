"""BL-R35-1 — rename holds ``_ensure_lock`` + pops orchestrator state.

``rename_project_handler`` ran its destructive sequence — ``systemctl
stop`` → ``os.rename(workspace)`` → ``_REGISTRY.rename`` — WITHOUT
holding ``_ensure_lock(old_name, "backend")`` and without popping the
old-name orchestrator state. So a concurrent ``_ensure(old_name)``
warm-start (a member/dashboard ``GET /app/<old>/`` →
``_schedule_backend_warm``) could, in the stop→os.rename window,
``systemctl start agent-mcp@old_name`` against the ALREADY-MOVED
workspace — a half-renamed / orphan backend the idle reaper only clears
after ``IDLE_SEC`` (~4 h).

This is the exact race delete closed via BL-R6-1 (holds the lock across
stop + registry mutation) and BL-R7-2 (pops ``last_active`` /
``active_conns`` / ``unit_start_times`` / boot-window for the name), but
rename was missed. These tests mirror
``test_sec_r6_warmstart_hardening.py`` / ``test_sec_r7_delete_lifecycle.py``
for rename semantics.

RED against origin/main: rename holds no lock (so the concurrent
warm-start starts an orphan) and never pops the old-name state.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
import time

import pytest


pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── BL-R35-1: lock held across the destructive sequence ─────────────


async def test_rename_holds_ensure_lock_during_stop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """While rename runs its ``systemctl stop``, ``_ensure_lock(old_name,
    "backend")`` MUST be held — otherwise a concurrent ``_ensure`` warm-
    start can start a backend against the moving workspace.

    White-box pin of BL-R6-1's rename sibling: observe ``lock.locked()``
    from inside the stubbed ``stop``. RED on origin/main (no lock held).
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("victim")
    from agent_mcp.router import project_orchestrator as _po

    lock = _po._ensure_lock("victim", "backend")
    observed: dict[str, bool] = {}

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "stop":
            observed["locked_during_stop"] = lock.locked()
        if verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _stub_systemctl)
    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/victim",
        json={"name": "renamed"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert observed.get("locked_during_stop") is True, (
        "rename must hold _ensure_lock(old_name, 'backend') across its "
        "stop→os.rename→registry.rename sequence (BL-R35-1) so a concurrent "
        "_ensure warm-start can't start a backend against the moved workspace"
    )


async def test_concurrent_warmstart_during_rename_does_not_start_backend(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """A ``_ensure(old_name)`` warm-start racing with an in-flight rename
    MUST NOT ``systemctl start`` the old-name unit.

    We park rename inside its ``systemctl stop`` (worker thread) so it is
    holding the lock mid-critical-section, then launch a warm-start and
    give it ample time to (wrongly) start. With the fix it blocks on the
    lock and, on acquiring it after rename renames the registry, 404s.
    RED on origin/main: rename holds no lock, so the warm-start starts the
    unit against the moving workspace (orphan until the idle reaper).
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("victim")
    from agent_mcp.router import project_orchestrator as _po

    unit = "agent-mcp@victim.service"
    stop_started = threading.Event()
    release_stop = threading.Event()
    started_units: list[str] = []

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        target = args[1] if len(args) > 1 else ""
        if verb == "stop":
            stop_started.set()
            release_stop.wait(timeout=5)
        elif verb in ("start", "restart"):
            started_units.append(target)
        elif verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _stub_systemctl)
    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)
    rename_task = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/victim",
            json={"name": "renamed"},
            headers=_ACCEPT,
        )
    )
    # Wait until rename is parked in its stop (holding the lock, if fixed).
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "rename never reached systemctl stop"

    # Launch the racing warm-start and give it time to reach systemctl start.
    ensure_task = asyncio.create_task(_po._ensure("victim", "backend"))
    await asyncio.sleep(0.2)

    # Let rename finish; then the warm-start (if it was blocked on the lock)
    # acquires it, re-checks the registry, and 404s.
    release_stop.set()
    resp = await rename_task
    assert resp.status == 200, await resp.text()

    # The warm-start either 404'd (HTTPNotFound) or is still parked on the
    # lock/to_thread when we cancel — suppress BOTH (CancelledError is a
    # BaseException, not an Exception, so ``suppress(Exception)`` misses it).
    ensure_task.cancel()
    with contextlib.suppress(BaseException):
        await ensure_task

    assert unit not in started_units, (
        "a warm-start racing an in-flight rename must NOT systemctl-start "
        "the old-name unit against the moving workspace (BL-R35-1 orphan)"
    )


# ── BL-R35-1: rename pops the old-name orchestrator state (BL-R7-2) ──


async def test_rename_pops_old_name_orchestrator_state(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After rename, the OLD name must be gone from every per-name
    orchestrator map — otherwise it lingers in ``list_active()`` and the
    ``_schedule_backend_warm`` dedup skips a same-name re-created project.
    Mirrors delete's ``test_delete_pops_project_from_last_active``.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("ephemeral")
    from agent_mcp.router import project_orchestrator as _po

    # Seed the per-name lifecycle maps exactly as a successful _ensure would.
    _po.last_active[("ephemeral", "backend")] = time.time()
    _po.unit_start_times[("ephemeral", "backend")] = time.monotonic()
    _po.forwarding_hmac_keys["ephemeral"] = b"x" * 32
    _po.ensure_failures[("ephemeral", "backend")] = (time.monotonic(), "old")

    assert ("ephemeral", "backend") in router_module.last_active

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/ephemeral",
        json={"name": "renamed"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("ephemeral", "backend") not in router_module.last_active
    assert ("ephemeral", "backend") not in _po.unit_start_times
    assert "ephemeral" not in _po.forwarding_hmac_keys
    assert ("ephemeral", "backend") not in _po.ensure_failures
    # And absent from the orchestrator's list_active() snapshot.
    names = {row["name"] for row in _po.ProjectOrchestrator(
        router_module._REGISTRY).list_active()}
    assert "ephemeral" not in names


async def test_rename_only_pops_the_target_project(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Renaming one project must NOT evict a sibling's lifecycle state."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("moving")
    register_project("stays")
    from agent_mcp.router import project_orchestrator as _po

    _po.last_active[("moving", "backend")] = time.time()
    _po.last_active[("stays", "backend")] = time.time()

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/moving",
        json={"name": "moved"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("moving", "backend") not in router_module.last_active
    assert ("stays", "backend") in router_module.last_active


# ── SC-R8-1 sibling: the old-name ensure lock is not leaked ─────────


async def test_rename_drops_old_name_ensure_lock(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After a successful rename, the OLD-name ``_ensure`` lock must be
    dropped from ``ensure_locks`` (create/rename of N names must not leak
    N Lock objects). Mirrors delete's SC-R8-1 pop.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("leaky")
    from agent_mcp.router import project_orchestrator as _po

    # Materialise the lock (as a warm-start would).
    _po._ensure_lock("leaky", "backend")
    assert ("leaky", "backend") in _po.ensure_locks

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/leaky",
        json={"name": "tidy"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("leaky", "backend") not in _po.ensure_locks, (
        "rename must drop the old-name _ensure lock after releasing it "
        "(SC-R8-1 sibling) — otherwise the Lock object leaks"
    )


# ── Parity (SC-3 sibling): rename purges the old-name runtime dir ────


async def test_rename_purges_old_name_runtime_dir(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After rename, the OLD-name runtime dir under SOCK_DIR (stale socket
    + forwarding_hmac key, preserved by ``RuntimeDirectoryPreserve=yes``)
    must be purged — delete does this (SC-3); rename didn't.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("runt")
    from agent_mcp.router import app as _app

    runtime_dir = _app.SOCK_DIR / "runt"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "backend.sock").write_bytes(b"")
    (runtime_dir / "forwarding_hmac").write_bytes(b"k" * 32)
    assert runtime_dir.is_dir()

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/runt",
        json={"name": "grown"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert not runtime_dir.exists(), (
        "rename must purge the old-name runtime dir (stale socket + HMAC "
        "key) — RuntimeDirectoryPreserve=yes leaves it after the stop"
    )
