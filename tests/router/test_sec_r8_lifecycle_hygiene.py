"""SEC round-8 lifecycle hygiene: ensure_locks cleanup + generic spawn body.

SC-R8-1: ``delete_project_handler`` pops five sibling lifecycle maps
(``last_active`` / ``ensure_failures`` / ``active_conns`` /
``unit_start_times`` / ``forwarding_hmac_keys``) but NOT ``ensure_locks``
— because it's HOLDING that lock across the stop+unregister. The
``async with _ensure_lock(name, "backend")`` itself get-or-creates the
lock entry, so create+delete of N distinct project names leaves N stale
``asyncio.Lock`` objects forever. Fix: pop the ``ensure_locks`` entry
AFTER releasing the lock. RED against origin/main: the entry lingers.

SC-R8-2: the ``_ensure`` systemctl-spawn-failure path returned the raw
systemd stderr (unit-file paths, "Failed at step EXEC …") to a caller
who can be any project member, not just a sysadmin. Fix: generic client
message ("backend failed to start"), full stderr logged server-side,
500 preserved. RED against origin/main: the raw stderr appears in the
response reason/body.
"""

from __future__ import annotations

import subprocess

import pytest
from aiohttp import web


pytestmark = pytest.mark.asyncio


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── SC-R8-1: delete pops the ensure_locks entry ─────────────────────


async def test_delete_pops_ensure_locks_entry(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """After create+delete, ``ensure_locks`` must have no entry for the
    project — otherwise a stale ``asyncio.Lock`` leaks per deleted name."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("lockleak")
    from agent_mcp.router import project_orchestrator as _po

    # Touch the lock the way a warm-start / prior _ensure would, so the
    # entry unambiguously exists going into the delete.
    _po._ensure_lock("lockleak", "backend")
    assert ("lockleak", "backend") in _po.ensure_locks

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/lockleak", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("lockleak", "backend") not in _po.ensure_locks, (
        "delete must pop the ensure_locks entry once it releases the lock "
        "— otherwise create+delete of N names leaks N asyncio.Lock objects"
    )
    # Same object is re-exported on the router module.
    assert ("lockleak", "backend") not in router_module.ensure_locks


async def test_delete_creates_no_lock_when_none_existed(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Even when no prior ``_ensure`` ran, the delete's own
    ``async with _ensure_lock(...)`` must not leave an entry behind."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("freshdel")
    from agent_mcp.router import project_orchestrator as _po

    assert ("freshdel", "backend") not in _po.ensure_locks

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/freshdel", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert ("freshdel", "backend") not in _po.ensure_locks


async def test_delete_only_pops_target_lock(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Deleting one project must not evict a sibling's ensure_lock."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("gonelk")
    register_project("stayslk")
    from agent_mcp.router import project_orchestrator as _po

    _po._ensure_lock("gonelk", "backend")
    _po._ensure_lock("stayslk", "backend")

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/gonelk", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    assert ("gonelk", "backend") not in _po.ensure_locks
    assert ("stayslk", "backend") in _po.ensure_locks


# ── SC-R8-2: spawn-failure body is generic, detail logged ───────────


_SECRET_STDERR = (
    "Failed at step EXEC spawning "
    "/etc/systemd/system/agent-mcp@spawnfail.service: No such file"
)


async def test_spawn_failure_response_is_generic(
    router_module, register_project, monkeypatch, caplog,
) -> None:
    """A systemctl start failure must yield a generic 500 body/reason —
    no raw stderr / unit-file paths — while the detail is logged."""
    register_project("spawnfail")
    from agent_mcp.router import project_orchestrator as _po

    _po._clear_ensure_failures()

    def _failing_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "is-active":
            # Report the unit inactive so _ensure chooses ``start``.
            return subprocess.CompletedProcess(list(args), 3, "", "")
        if verb == "start":
            return subprocess.CompletedProcess(
                list(args), 1, "", _SECRET_STDERR,
            )
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _failing_systemctl)
    monkeypatch.setattr(router_module, "_systemctl", _failing_systemctl)

    with caplog.at_level("ERROR"):
        with pytest.raises(web.HTTPInternalServerError) as excinfo:
            await _po._ensure("spawnfail", "backend")

    exc = excinfo.value
    # HTTP status preserved.
    assert exc.status == 500
    # Neither the status-line reason nor the body reflects the stderr.
    assert _SECRET_STDERR not in (exc.reason or "")
    assert _SECRET_STDERR not in (exc.text or "")
    assert "EXEC" not in (exc.text or "")
    assert "/etc/systemd" not in (exc.text or "")
    assert "/etc/systemd" not in (exc.reason or "")

    # The detail IS captured server-side.
    assert any(
        _SECRET_STDERR in rec.getMessage() for rec in caplog.records
    ), "full stderr must be logged server-side"


async def test_spawn_failure_cooldown_replay_is_generic(
    router_module, register_project, monkeypatch, caplog,
) -> None:
    """The cooldown-cached failure replayed to the next caller (a 504)
    must also be generic — the stored reason must not carry stderr."""
    register_project("spawnfail2")
    from agent_mcp.router import project_orchestrator as _po

    _po._clear_ensure_failures()
    monkeypatch.setattr(_po, "ENSURE_FAILURE_COOLDOWN_SEC", 60.0)

    def _failing_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        if verb == "start":
            return subprocess.CompletedProcess(
                list(args), 1, "", _SECRET_STDERR,
            )
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _failing_systemctl)
    monkeypatch.setattr(router_module, "_systemctl", _failing_systemctl)

    with pytest.raises(web.HTTPInternalServerError):
        await _po._ensure("spawnfail2", "backend")

    # Entry cached under cooldown; the stored reason must be generic.
    failed_at, reason = _po.ensure_failures[("spawnfail2", "backend")]
    assert _SECRET_STDERR not in reason
    assert "/etc/systemd" not in reason
    assert isinstance(failed_at, float)

    # Next call within the window replays a 504 with the same generic
    # reason (never the stderr).
    with pytest.raises(web.HTTPGatewayTimeout) as excinfo:
        await _po._ensure("spawnfail2", "backend")
    assert _SECRET_STDERR not in (excinfo.value.reason or "")
