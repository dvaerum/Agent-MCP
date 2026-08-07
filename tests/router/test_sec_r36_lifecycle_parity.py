"""R36 lifecycle-handler parity — stop holds the lock + clears state,
rename re-validates existence/collision INSIDE the lock.

Two findings, one class-sweep:

* **BL-R36-1** — ``stop_project_handler`` stopped the systemd unit but,
  unlike delete / rename / ``ProjectOrchestrator.stop()``, held NO
  ``_ensure_lock(name, "backend")`` and popped NONE of the per-name
  orchestrator lifecycle maps. Result: after a stop, ``last_active``
  keeps the pre-stop timestamp (overview shows ``status:"stopped"`` with
  a stale ``last_activity_ts``), the ``_schedule_backend_warm`` dedup is
  suppressed, and the SC-R7-1 boot-grace window reopens; a concurrent
  ``_ensure`` warm-start can also race the stop.

* **PF-R36-1** — two concurrent ``PATCH`` renames of the SAME project:
  #389's ``_ensure_lock`` serialises them, but ``rename_project_handler``
  never RE-CHECKED existence / alias-collision INSIDE the lock. The
  losing racer (the winner already renamed the project away) called
  ``_REGISTRY.rename(old_name, …)`` → ``KeyError`` → mapped to a 500
  "registry rename failed" instead of the clean 404 its own outside-lock
  probe already computes.

These mirror delete's BL-R6-1 (lock) / BL-R7-2 (state-pop) and the
standard re-validate-inside-the-lock TOCTOU pattern.

RED against the pre-fix tree: stop holds no lock + leaves stale state;
the losing concurrent rename 500s.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time

import pytest

pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── BL-R36-1: stop clears the per-name orchestrator state ───────────


async def test_stop_pops_project_orchestrator_state(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After a stop, every per-name orchestrator map must be cleared —
    otherwise ``last_active`` keeps a stale ``last_activity_ts`` (overview
    shows stopped-but-recently-active) and the warm-start dedup is
    suppressed. Mirrors ``ProjectOrchestrator.stop()`` / delete's
    BL-R7-2.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("winddown")
    from agent_mcp.router import project_orchestrator as _po

    # Seed the per-name lifecycle maps exactly as a successful _ensure would.
    _po.last_active[("winddown", "backend")] = time.time()
    _po.unit_start_times[("winddown", "backend")] = time.monotonic()
    _po.forwarding_hmac_keys["winddown"] = b"x" * 32
    _po.ensure_failures[("winddown", "backend")] = (time.monotonic(), "old")

    assert ("winddown", "backend") in router_module.last_active

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects/winddown/stop",
        data="{}",
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("winddown", "backend") not in router_module.last_active
    assert ("winddown", "backend") not in _po.unit_start_times
    assert "winddown" not in _po.forwarding_hmac_keys
    assert ("winddown", "backend") not in _po.ensure_failures
    # And absent from the orchestrator's list_active() snapshot (this is
    # what the overview endpoint renders as last_activity_ts).
    names = {row["name"] for row in _po.ProjectOrchestrator(
        router_module._REGISTRY).list_active()}
    assert "winddown" not in names


async def test_stop_only_pops_the_target_project(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Stopping one project must NOT evict a sibling's lifecycle state."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("stopped_one")
    register_project("keeps")
    from agent_mcp.router import project_orchestrator as _po

    _po.last_active[("stopped_one", "backend")] = time.time()
    _po.last_active[("keeps", "backend")] = time.time()

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects/stopped_one/stop",
        data="{}",
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("stopped_one", "backend") not in router_module.last_active
    assert ("keeps", "backend") in router_module.last_active


async def test_stop_holds_ensure_lock_during_systemctl_stop(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """While stop runs its ``systemctl stop``, ``_ensure_lock(name,
    "backend")`` MUST be held so a concurrent ``_ensure`` warm-start can't
    interleave. White-box pin of delete's BL-R6-1 sibling: observe
    ``lock.locked()`` from inside the stubbed ``stop``. The unit is marked
    active so the handler actually calls ``stop``.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("busy")
    from agent_mcp.router import project_orchestrator as _po

    lock = _po._ensure_lock("busy", "backend")
    observed: dict[str, bool] = {}

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "is-active":
            # Report ACTIVE so stop_project_handler proceeds to stop.
            return subprocess.CompletedProcess(list(args), 0, "", "")
        if verb == "stop":
            observed["locked_during_stop"] = lock.locked()
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects/busy/stop",
        data="{}",
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert observed.get("locked_during_stop") is True, (
        "stop must hold _ensure_lock(name, 'backend') across its systemctl "
        "stop (BL-R36-1) so a concurrent _ensure warm-start can't interleave"
    )


# ── PF-R36-1: concurrent rename → loser 404, never a 500 ────────────


async def test_concurrent_rename_loser_returns_404_not_500(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Two concurrent renames of the SAME project. The #389 lock serialises
    them; the winner renames the project away. The loser, on acquiring the
    lock, must RE-CHECK existence INSIDE the lock and return the clean 404
    — NOT let ``_REGISTRY.rename`` KeyError escape to a 500.

    We park the winner inside its ``systemctl stop`` (holding the lock),
    launch the loser (which passes its outside-lock probe while the project
    still exists, then blocks on the lock), release the winner, and assert
    the loser gets 404, never 500.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("contended")
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "stop":
            stop_started.set()
            release_stop.wait(timeout=5)
        elif verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)

    # Winner: contended -> winner. Parks in its systemctl stop (holds lock).
    winner = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/contended",
            json={"name": "winner"},
            headers=_ACCEPT,
        )
    )
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "winner rename never reached systemctl stop"

    # Loser: contended -> loser. Passes its outside-lock probe (contended
    # still registered — winner is parked BEFORE its registry.rename), then
    # blocks on _ensure_lock('contended','backend').
    loser = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/contended",
            json={"name": "loser"},
            headers=_ACCEPT,
        )
    )
    await asyncio.sleep(0.2)  # give the loser time to reach the lock

    # Let the winner finish its rename (contended -> winner), release lock.
    release_stop.set()
    winner_resp = await winner
    assert winner_resp.status == 200, await winner_resp.text()

    loser_resp = await loser
    assert loser_resp.status != 500, (
        "the losing concurrent rename must not surface a 500 — the "
        "_REGISTRY.rename KeyError (contended already renamed away) must be "
        "caught by an inside-lock existence re-check (PF-R36-1)"
    )
    assert loser_resp.status == 404, (
        f"the losing concurrent rename should return the clean 404 its "
        f"outside-lock probe computes, got {loser_resp.status}: "
        f"{await loser_resp.text()}"
    )


async def test_rename_revalidates_alias_collision_inside_lock(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """If ``new_name`` becomes an active alias of another project between
    the outside-lock probe and the inside-lock re-check, rename must return
    409 alias_collision from INSIDE the lock — not proceed into the
    destructive rename. White-box: flip ``resolve_alias`` to pass outside
    and collide inside.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("mover")
    from agent_mcp.router import app as _app

    real_resolve = _app._REGISTRY.resolve_alias
    calls = {"n": 0}

    def _flapping_resolve(alias: str):
        # The rename handler probes resolve_alias(new_name) once outside the
        # lock and once inside. Pass outside (None), collide inside.
        if alias == "target":
            calls["n"] += 1
            if calls["n"] >= 2:
                return "someoneelse"
            return None
        return real_resolve(alias)

    monkeypatch.setattr(_app._REGISTRY, "resolve_alias", _flapping_resolve)

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/mover",
        json={"name": "target"},
        headers=_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body.get("error") == "alias_collision", body
    assert calls["n"] >= 2, (
        "rename must re-check resolve_alias(new_name) INSIDE the lock "
        "(PF-R36-1), not only outside"
    )
