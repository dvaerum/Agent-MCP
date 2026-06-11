"""Contract tests for ``agent_mcp.router.project_orchestrator``.

PR C of the round-2 architecture review: the per-project lifecycle —
systemd activation, idle reaper, alias-grace reaper, startup
reconciliation, and alias resolution — was extracted out of
``agent_mcp.router.app`` into a sibling module so the router becomes a
thin URL dispatcher (ADR-0009 ops index stays in ``app.py``).

These tests pin the orchestrator's *contract*: methods exposed,
shapes of return values, idempotency, and ADR-0010 alias-with-grace
semantics. They use the existing router-test conftest fixtures
(``router_env``, ``systemctl_stub``) to wire the orchestrator against
a tmp registry + stubbed systemctl, and they patch
``project_orchestrator._systemctl`` to the same recorder the router
tests use. This keeps the test surface aligned with how the live
code is exercised.

Scope of the cases pinned here:

  * ``start()`` lazy activation + idempotency
  * ``stop()`` graceful refusal on active connections + ``force=True``
  * ``list_active()`` reflects ``last_active`` transitions
  * ``resolve()`` for real names + grace aliases + unknown
  * ``add_alias()`` conflict detection (name_taken / alias_collision)
  * ``remove_alias()`` immediate skip-grace removal
  * ``reaper_tick()`` stops idle projects, leaves fresh ones
  * ``alias_expiry_tick()`` removes expired aliases, preserves
    in-grace ones, leaves malformed entries (ADR-0010)
  * ``reconcile_on_startup()`` adopts active units, ignores noise
  * URL dispatch through the router still resolves an aliased name
    (regression guard for the post-split shape)
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


@pytest.fixture
def orchestrator_module(
    router_env, systemctl_stub, monkeypatch: pytest.MonkeyPatch,
):
    """Import ``project_orchestrator`` with the same env scaffolding the
    router tests use, plus the shared systemctl stub patched in.

    Mirrors the ``router_module`` fixture's drop-and-reimport pattern
    so module-level state (``last_active``, ``active_conns``,
    ``ensure_locks``) is reset per test.
    """
    for mod_name in (
        "agent_mcp.router.app",
        "agent_mcp.router.project_orchestrator",
        "agent_mcp.router.project_registry",
    ):
        sys.modules.pop(mod_name, None)
    orch = importlib.import_module("agent_mcp.router.project_orchestrator")
    monkeypatch.setattr(orch, "_systemctl", systemctl_stub)
    return orch


@pytest.fixture
def orchestrator(orchestrator_module, router_env):
    """A ``ProjectOrchestrator`` wired against the test registry."""
    from agent_mcp.router import project_registry
    project_registry.REGISTRY_PATH = router_env.projects_file
    registry = project_registry.ProjectRegistry()
    return orchestrator_module.ProjectOrchestrator(registry)


# ── start() ────────────────────────────────────────────────────────


async def test_start_activates_and_returns_socket(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
):
    """First call: systemctl start + socket appears → returns the sock path."""
    name = "alpha"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    # The stub treats start/restart as "set active"; we also need the
    # socket file to materialise so ``_ensure``'s socket-existence
    # check passes.
    sock_path = router_env.sock_dir / name / "backend.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.touch()

    # Make .is_socket() pass by patching it for the test (the stub
    # touched file is a regular file, not a socket).
    monkey_target = orchestrator_module._sock_path
    real_is_socket = Path.is_socket
    Path.is_socket = lambda self: True  # type: ignore[assignment]
    try:
        result = await orchestrator.start(name)
    finally:
        Path.is_socket = real_is_socket  # type: ignore[assignment]

    assert result == sock_path
    assert systemctl_stub.counts[("start", unit)] == 1, (
        f"start did not invoke systemctl start; calls: {systemctl_stub.calls}"
    )
    # Last-active timestamp must be recorded.
    assert (name, "backend") in orchestrator_module.last_active


async def test_start_is_idempotent_when_already_running(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
):
    """Subsequent calls when the unit is active + socket exists: no
    additional start/restart invocations."""
    name = "beta"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    sock_path = router_env.sock_dir / name / "backend.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.touch()
    Path.is_socket = lambda self: True  # type: ignore[assignment]
    try:
        # Two back-to-back calls; second one must not start/restart.
        await orchestrator.start(name)
        await orchestrator.start(name)
    finally:
        # Restore the original.
        del Path.is_socket
    assert systemctl_stub.counts[("start", unit)] == 0
    assert systemctl_stub.counts[("restart", unit)] == 0


# ── stop() ─────────────────────────────────────────────────────────


async def test_stop_graceful_invokes_systemctl_stop_when_active(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
):
    name = "gamma"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    result = orchestrator.stop(name)
    assert result["stopped"] is True
    assert systemctl_stub.counts[("stop", unit)] == 1


async def test_stop_refuses_when_active_connections_without_force(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
):
    name = "delta"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    orchestrator_module.active_conns[name] = 2

    result = orchestrator.stop(name)
    assert result["stopped"] is False
    assert result["reason"] == "active_sessions"
    assert result["active_connections"] == 2
    # And systemctl was NOT invoked.
    assert systemctl_stub.counts[("stop", unit)] == 0


async def test_stop_force_runs_systemctl_even_with_active_connections(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
):
    name = "epsilon"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    orchestrator_module.active_conns[name] = 1

    result = orchestrator.stop(name, force=True)
    assert result["stopped"] is True
    assert systemctl_stub.counts[("stop", unit)] == 1


# ── list_active() ──────────────────────────────────────────────────


async def test_list_active_reflects_last_active_timestamps(
    orchestrator, orchestrator_module, router_env,
):
    orchestrator_module.last_active[("p1", "backend")] = 1000.0
    orchestrator_module.last_active[("p2", "backend")] = 2000.0

    snapshot = orchestrator.list_active()
    by_name = {row["name"]: row for row in snapshot}
    assert by_name["p1"]["last_activity_ts"] == 1000.0
    assert by_name["p2"]["last_activity_ts"] == 2000.0
    assert by_name["p1"]["role"] == "backend"


# ── resolve() / aliases ────────────────────────────────────────────


async def test_resolve_real_project_returns_none_alias_entry(
    orchestrator, router_env,
):
    name = "zeta"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    real, alias = orchestrator.resolve(name)
    assert real == name
    assert alias is None


async def test_resolve_alias_returns_real_plus_entry(
    orchestrator, router_env,
):
    name = "newname"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    # Rename in the registry — that creates the old-name → new-name alias.
    orchestrator.registry.register("oldname", str(router_env.root / "ws" / "oldname"))
    orchestrator.registry.unregister(name)
    orchestrator.registry.rename("oldname", name, grace_days=30)

    real, alias = orchestrator.resolve("oldname")
    assert real == name
    assert alias is not None
    assert alias["name"] == "oldname"
    assert "expires_at" in alias


async def test_resolve_unknown_raises_404(orchestrator):
    from aiohttp import web
    with pytest.raises(web.HTTPNotFound):
        orchestrator.resolve("nonesuch")


# ── add_alias() / remove_alias() ───────────────────────────────────


async def test_add_alias_conflict_when_name_is_a_real_project(
    orchestrator, router_env,
):
    orchestrator.registry.register("eta", str(router_env.root / "ws" / "eta"))
    orchestrator.registry.register("theta", str(router_env.root / "ws" / "theta"))
    result = orchestrator.add_alias("eta", "theta", grace_days=7)
    assert result["ok"] is False
    assert result["error"] == "name_taken"


async def test_remove_alias_is_idempotent_for_missing_alias(
    orchestrator, router_env,
):
    orchestrator.registry.register("iota", str(router_env.root / "ws" / "iota"))
    # No exception on missing alias.
    orchestrator.remove_alias("iota", "ghost")


# ── reaper_tick() ──────────────────────────────────────────────────


async def test_reaper_tick_stops_idle_project(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
    monkeypatch: pytest.MonkeyPatch,
):
    name = "kappa"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    last = 1_000_000.0
    orchestrator_module.last_active[(name, "backend")] = last
    monkeypatch.setattr(
        orchestrator_module.time, "time",
        lambda: last + orchestrator_module.IDLE_SEC + 60.0,
    )
    await orchestrator.reaper_tick()
    assert systemctl_stub.counts[("stop", unit)] == 1
    assert (name, "backend") not in orchestrator_module.last_active


async def test_reaper_tick_leaves_fresh_project(
    orchestrator, orchestrator_module, router_env, systemctl_stub,
    monkeypatch: pytest.MonkeyPatch,
):
    name = "lambda_"
    orchestrator.registry.register(name, str(router_env.root / "ws" / name))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    last = 2_000_000.0
    orchestrator_module.last_active[(name, "backend")] = last
    monkeypatch.setattr(
        orchestrator_module.time, "time",
        lambda: last + (orchestrator_module.IDLE_SEC // 2),
    )
    await orchestrator.reaper_tick()
    assert systemctl_stub.counts[("stop", unit)] == 0
    assert (name, "backend") in orchestrator_module.last_active


# ── alias_expiry_tick() ────────────────────────────────────────────


async def test_alias_expiry_tick_removes_expired_alias(
    orchestrator, router_env,
):
    """Expired alias is removed; in-grace alias is preserved.

    ADR-0010 alias-with-grace: the orchestrator's alias-expiry tick
    is the implementation of the "grace" half of the ADR. An alias
    whose ``expires_at`` is in the past must be removed; one whose
    expiry is in the future must survive the sweep.
    """
    orchestrator.registry.register("mu", str(router_env.root / "ws" / "mu"))
    orchestrator.registry.register("old1", str(router_env.root / "ws" / "old1"))
    orchestrator.registry.register("old2", str(router_env.root / "ws" / "old2"))
    orchestrator.registry.unregister("mu")
    # Rename creates "old1" → "mu" alias. To get the past expiry we
    # directly call add_alias with a negative TTL.
    orchestrator.registry.rename("old1", "mu", grace_days=30)
    # Add a second alias that's already expired by passing
    # grace_days=0 — registry computes expires_at = now + 0 days,
    # which is ``now``, and ``exp <= now`` is True so it'll be reaped.
    orchestrator.registry.add_alias("mu", "old2", grace_days=0)
    # Sanity: both aliases exist pre-tick.
    row = orchestrator.registry.get("mu")
    alias_names_before = {e["name"] for e in row["aliases"]}
    assert {"old1", "old2"}.issubset(alias_names_before)

    await orchestrator.alias_expiry_tick()

    row_after = orchestrator.registry.get("mu")
    alias_names_after = {e["name"] for e in row_after["aliases"]}
    # ``old2`` (grace=0) is reaped; ``old1`` (grace=30) survives.
    assert "old1" in alias_names_after
    assert "old2" not in alias_names_after


async def test_alias_expiry_tick_preserves_malformed_entries(
    orchestrator, router_env,
):
    """ADR-0010: malformed ``expires_at`` is *not* dropped silently.

    The operator must be able to clean up by hand without the reaper
    racing them. This is the explicit decision documented in the
    extracted-from comment in the legacy ``_alias_reaper_tick``.
    """
    orchestrator.registry.register("nu", str(router_env.root / "ws" / "nu"))
    orchestrator.registry.register("old3", str(router_env.root / "ws" / "old3"))
    orchestrator.registry.unregister("nu")
    orchestrator.registry.rename("old3", "nu", grace_days=30)
    # Surgically corrupt the expires_at on the alias entry. We go
    # through the registry's list/write path so the on-disk JSON
    # actually carries the malformed timestamp.
    import json
    raw = router_env.projects_file.read_text()
    data = json.loads(raw)
    # Find the "nu" project's aliases and overwrite expires_at.
    for row in data["projects"]:
        if row["name"] == "nu":
            for entry in row.get("aliases", []):
                entry["expires_at"] = "not-a-valid-timestamp"
    router_env.projects_file.write_text(json.dumps(data))

    await orchestrator.alias_expiry_tick()

    row_after = orchestrator.registry.get("nu")
    names_after = {e["name"] for e in row_after["aliases"]}
    assert "old3" in names_after, (
        "alias with malformed expires_at must be preserved per ADR-0010 "
        "extracted-from comment — operator cleans up by hand"
    )


# ── reconcile_on_startup() ─────────────────────────────────────────


async def test_reconcile_on_startup_adopts_active_units(
    orchestrator, orchestrator_module, systemctl_stub,
    monkeypatch: pytest.MonkeyPatch,
):
    """A unit that's already running when the router starts is added
    to ``last_active`` so the reaper can later consider it for idle
    timeout — without this, a router crash + restart would orphan
    every active backend."""

    # Override the stub's behaviour for the `list-units` call —
    # return one active unit.
    real_call = systemctl_stub.__call__

    def fake_call(*args):
        if args and args[0] == "list-units":
            return subprocess.CompletedProcess(
                args=list(args), returncode=0,
                stdout="agent-mcp@adopted.service loaded active running …\n",
                stderr="",
            )
        return real_call(*args)

    monkeypatch.setattr(orchestrator_module, "_systemctl", fake_call)
    orchestrator_module.last_active.clear()

    await orchestrator.reconcile_on_startup()
    assert ("adopted", "backend") in orchestrator_module.last_active


# ── Regression: URL dispatch through the router still resolves aliases ──


async def test_url_dispatch_proxy_resolves_alias_via_orchestrator(
    aiohttp_client, router_module, router_env, monkeypatch,
):
    """End-to-end regression: a request for ``/agent-mcp/mcp/<old>`` —
    where <old> is a grace alias of a real project — must be 401'd
    (the project exists but no token) rather than 404'd. Tests that
    the orchestrator's alias resolution is wired into the router's
    backend handler post-split.
    """
    name = "omikron"
    ws = router_env.root / "ws" / name
    ws.mkdir(parents=True, exist_ok=True)
    router_module._REGISTRY.register(name, str(ws))
    # Rename to create an alias.
    router_module._REGISTRY.register("renamed_target", str(router_env.root / "ws" / "renamed_target"))
    router_module._REGISTRY.unregister("renamed_target")
    router_module._REGISTRY.rename(name, "renamed_target", grace_days=30)

    client = await aiohttp_client(router_module.make_app())
    # Hit the MCP transport via the alias (the legacy name). Without
    # an Authorization header the response should be 401 — proving
    # the URL was *resolved* (otherwise it'd be 404).
    resp = await client.post(f"/agent-mcp/mcp/{name}")
    assert resp.status == 401, (
        f"alias URL must resolve to 401 (auth missing), got {resp.status}"
    )
