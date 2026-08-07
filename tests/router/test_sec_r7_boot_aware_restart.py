"""SC-R7-1 / SC-R7-2 — boot-aware ``_ensure`` restart + ``_systemctl`` timeout.

SC-R7-1 (FLAG-2 root cause): the backend cold boot (~44 s of embedding /
DB init) far exceeds ``ENSURE_FAILURE_COOLDOWN_SEC`` (5 s). A
``Type=simple`` unit is ``active`` the instant its process forks — long
before the backend binds its UDS — so ``_ensure`` sees "active but
socketless" and used to pick ``restart`` unconditionally. A member
polling ``GET /api/<proj>/…`` every ≥5 s during a cold boot then
re-restarted the still-booting backend on every call, resetting its
~44 s clock so it never reached ready: an authenticated, same-project
availability DoS.

The fix makes the restart decision boot-aware: an ``active``-but-
socketless unit WITHIN a boot-grace window (``BOOT_GRACE_SEC``) is
"still booting — keep waiting for the socket", not "stale — restart".
Only an inactive/failed unit (start), or an ``active`` unit PAST the
grace window (restart), triggers a systemctl action.

SC-R7-2: ``_systemctl`` shells out via ``subprocess.run`` with no
``timeout=``; a D-Bus stall would pin the worker until systemd's own
timeout. The fix adds a ``timeout=`` and surfaces a
``TimeoutExpired`` as a failed systemctl action (non-zero return, no
crash).

These tests are RED against origin/main: the restart-within-grace
suppression and the timeout handling don't exist there.
"""

from __future__ import annotations

import subprocess
import time

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio


# ── SC-R7-1: boot-aware restart ─────────────────────────────────────


async def test_active_socketless_within_grace_is_not_restarted(
    router_module, router_env, systemctl_stub,
) -> None:
    """An ``active`` unit with no socket, started within the boot-grace
    window, MUST NOT be restarted — ``_ensure`` keeps polling the
    socket. This is the FLAG-2 livelock fix: no restart storm while a
    cold boot is in progress.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "booting"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    unit = f"agent-mcp@{name}.service"
    # Unit is active (process forked) but the UDS hasn't appeared yet,
    # and we recorded its start as "just now" (well within grace).
    systemctl_stub.active_units.add(unit)
    _po.unit_start_times[(name, "backend")] = time.monotonic()

    # AGENT_MCP_ENSURE_SOCKET_ATTEMPTS=1 in the test env → one ~0.1 s
    # poll, then the not-ready 504 (the socket never appears here).
    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")

    assert systemctl_stub.counts[("restart", unit)] == 0, (
        "an active-but-socketless unit within the boot-grace window must "
        "NOT be restarted — that is the FLAG-2 cold-boot livelock"
    )
    assert systemctl_stub.counts[("start", unit)] == 0, (
        "must not (re)start an already-active, still-booting unit"
    )


async def test_active_socketless_no_record_is_adopted_not_restarted(
    router_module, router_env, systemctl_stub,
) -> None:
    """An ``active``-but-socketless unit with NO recorded start time is
    ADOPTED as starting "now" (given the full grace window), not
    restarted immediately.

    This is the safe default for a unit we didn't start (router
    restarted and lost the map, or systemd autonomously restarted via
    ``Restart=on-failure``): favour not disrupting a possibly-booting
    backend. A genuinely stale unit adopted this way is still restarted
    once the grace elapses on a later call (covered below).
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "adopt-me"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    _po.unit_start_times.pop((name, "backend"), None)  # no record

    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")

    assert systemctl_stub.counts[("restart", unit)] == 0
    # And it recorded a start window so subsequent calls measure grace
    # from this first observation.
    assert (name, "backend") in _po.unit_start_times


async def test_active_socketless_past_grace_is_restarted(
    router_module, router_env, systemctl_stub, monkeypatch,
) -> None:
    """An ``active``-but-socketless unit PAST the boot-grace window is
    genuinely stale (e.g. crashed mid-write) and MUST be restarted.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "stale"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)
    # Shrink the grace so the test is fast, then place the start well
    # before it.
    monkeypatch.setattr(_po, "BOOT_GRACE_SEC", 1.0)
    _po.unit_start_times[(name, "backend")] = time.monotonic() - 100.0

    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")

    assert systemctl_stub.counts[("restart", unit)] == 1, (
        "an active-but-socketless unit past the boot-grace window is "
        "stale and must be restarted"
    )


async def test_inactive_unit_is_started_regardless_of_grace(
    router_module, router_env, systemctl_stub,
) -> None:
    """A genuinely-dead (inactive) unit is started promptly — the
    boot-grace logic must not delay bringing up a stopped backend.
    """
    name = "dead"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()
    unit = f"agent-mcp@{name}.service"
    # Not in active_units → is-active reports inactive.

    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")

    assert systemctl_stub.counts[("start", unit)] == 1, (
        "an inactive unit must be started promptly (never restarted)"
    )
    assert systemctl_stub.counts[("restart", unit)] == 0


async def test_boot_grace_default_covers_cold_boot_and_socket_wait(
    router_module,
) -> None:
    """The default boot-grace budget must exceed both the cold-boot
    time (~44 s) and the production socket-wait (~20 s) so a single
    caller's own socket-wait never trips the grace into a restart.
    """
    # The router env fixture does not override AGENT_MCP_BOOT_GRACE_SEC,
    # so we see the module default.
    assert router_module._po.BOOT_GRACE_SEC >= 44.0


# ── SC-R7-2: _systemctl timeout ─────────────────────────────────────


async def test_systemctl_timeout_is_handled_as_failed_action(
    router_env, monkeypatch,
) -> None:
    """A ``subprocess.TimeoutExpired`` from the underlying ``systemctl``
    call is caught and surfaced as a failed action (non-zero return,
    single-line stderr) — the worker must not crash with
    ``TimeoutExpired``.

    Uses ``router_env`` (not ``router_module``) so ``_po._systemctl`` is
    the REAL function, not the recording stub, and monkeypatches the
    underlying ``subprocess.run``.
    """
    from agent_mcp.router import project_orchestrator as _po

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=kwargs.get("timeout", 1),
        )

    monkeypatch.setattr(_po.subprocess, "run", _raise_timeout)

    result = _po._systemctl("start", "agent-mcp@x.service")
    assert result.returncode != 0, (
        "a systemctl timeout must be reported as a FAILED action"
    )
    assert "\n" not in (result.stderr or ""), (
        "stderr must be a single line (fed into an HTTP reason downstream)"
    )
    assert "timed out" in (result.stderr or "").lower()


async def test_systemctl_passes_timeout_to_subprocess(
    router_env, monkeypatch,
) -> None:
    """``_systemctl`` must pass a ``timeout=`` to ``subprocess.run`` so a
    D-Bus stall can't pin the worker until systemd's own timeout.
    """
    from agent_mcp.router import project_orchestrator as _po

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(_po.subprocess, "run", _fake_run)
    _po._systemctl("is-active", "agent-mcp@x.service")
    assert seen.get("timeout"), "_systemctl must pass timeout= to subprocess.run"
    assert seen["timeout"] > 0
