"""Round-6 hardening of the FLAG-1 #319 dashboard ``/app/`` warm-start.

#319 made ``GET /agent-mcp/app/<name>/`` (the static SPA shell, no JS)
lazily spawn the per-project backend via ``_ensure`` so a non-browser
caller (curl, a smoke test) still triggers the documented "spawn on
first request" behaviour. Round-6 review flagged four ways that
side-effect was under-guarded:

  * **SC-R6-1** cross-tenant non-member spawn — the auth middleware
    serves the SPA shell to authenticated NON-MEMBERS (round-4 oracle
    fix), and ``_ensure`` only checks project EXISTENCE, so any
    authenticated operator could activate ANY tenant's backend via a
    plain ``GET /app/<victim>/``. Fix: gate the warm-start on the
    middleware's ``_warm_authorized`` flag. The response stays a
    uniform 200 shell either way (no oracle) — only the spawn is gated.
  * **BL-R6-1** TOCTOU — a warm-start that passes the registry check
    then blocks on ``_ensure_lock`` could ``systemctl start`` a project
    a concurrent DELETE just unregistered, orphaning a backend until
    the idle reaper (~4 h). Fix: re-check the registry INSIDE the lock,
    immediately before the spawn.
  * **BL-R6-2b** off-loop systemctl — ``_systemctl`` is a synchronous
    ``subprocess.run``; #319's detached task means its ~15-150 ms of
    blocking stalls other tenants on the single event loop. Fix: run it
    via ``asyncio.to_thread``.
  * **BL-R6-2a** dedup — one untracked warm task per GET floods the
    task set. Fix: dedup per project name.

These tests pin all four. The SC-R6-1 non-member assertion and the
dedup assertion are RED against origin/main (#319).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
import time

import pytest
from aiohttp import web

from tests.harness import assert_ran_off_event_loop


pytestmark = pytest.mark.asyncio


# ── Helpers (mirror tests/router/test_dashboard_session_auth.py) ────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


async def _login(client, username: str, password: str = "pw") -> str:
    """POST /agent-mcp/login and return the session cookie value."""
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie on successful login"
    name_val = set_cookie.split(";", 1)[0]
    name, _, value = name_val.partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


# ── SC-R6-1: membership-gated warm-start (no cross-tenant spawn) ────


@pytest.mark.no_auth_seed_session
async def test_non_member_app_get_serves_shell_but_does_not_warm(
    aiohttp_client, router_module, router_app, register_project,
    write_dashboard_file, monkeypatch,
) -> None:
    """A non-member's ``GET /app/<name>/`` returns the 200 SPA shell but
    MUST NOT schedule a backend warm-start.

    SC-R6-1: the shell is served to authenticated non-members to avoid
    a project-existence oracle (round-4), so the spawn side-effect —
    not the response — is what must be authorization-gated. RED against
    #319, which schedules the warm unconditionally.
    """
    write_dashboard_file("index.html", "<!doctype html><html></html>")
    register_project("shared")  # seeds sentinel (sysadmin) + membership
    identity = _identity_module()
    identity.create_user(username="bob", password="pw")  # non-member

    calls: list[str] = []
    monkeypatch.setattr(
        router_module, "_schedule_backend_warm",
        lambda req, name: calls.append(name),
    )
    client = await aiohttp_client(router_app)
    bob_cookie = await _login(client, "bob")

    resp = await client.get(
        "/agent-mcp/app/shared/",
        cookies={"agent_mcp_session": bob_cookie},
        headers={"Accept": "text/html"},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert calls == [], (
        "a non-member GET /app/shared/ must NOT warm-start the backend "
        "— that is a cross-tenant resource-activation / DoS vector"
    )


@pytest.mark.no_auth_seed_session
async def test_member_app_get_serves_shell_and_warms(
    aiohttp_client, router_module, router_app, register_project,
    write_dashboard_file, monkeypatch,
) -> None:
    """An authorized member's ``GET /app/<name>/`` returns the same 200
    shell AND schedules the warm-start.

    Paired with the non-member test: both surfaces answer 200 (no
    oracle); only the authorized member triggers the spawn. Uses a
    non-sysadmin member so the middleware's membership branch (not the
    sysadmin branch) is what sets ``_warm_authorized``.
    """
    write_dashboard_file("index.html", "<!doctype html><html></html>")
    register_project("shared")  # seeds sentinel (sysadmin) first
    identity = _identity_module()
    carol_id = identity.create_user(username="carol", password="pw")
    identity.add_project_membership(carol_id, "shared")

    calls: list[str] = []
    monkeypatch.setattr(
        router_module, "_schedule_backend_warm",
        lambda req, name: calls.append(name),
    )
    client = await aiohttp_client(router_app)
    carol_cookie = await _login(client, "carol")

    resp = await client.get(
        "/agent-mcp/app/shared/",
        cookies={"agent_mcp_session": carol_cookie},
        headers={"Accept": "text/html"},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert calls == ["shared"], (
        "an authorized member GET /app/shared/ must warm-start the "
        "backend (the documented lazy-spawn on first request)"
    )


# ── BL-R6-1: TOCTOU — delete during lock aborts the spawn ───────────


async def test_ensure_aborts_when_project_deleted_while_lock_held(
    router_module, systemctl_stub, register_project, monkeypatch,
) -> None:
    """If the project is unregistered while ``_ensure`` holds
    ``_ensure_lock``, the spawn MUST be aborted — no ``systemctl start``
    for a project that no longer exists.

    Simulated by unregistering the project from inside
    ``ensure_forwarding_hmac_key``, which ``_ensure`` calls in the
    critical section immediately before the registry re-check. RED
    against #319, which has no inside-lock re-check and starts the unit
    anyway (orphan until the idle reaper).
    """
    from agent_mcp.router import project_orchestrator as _po

    register_project("victim")
    unit = "agent-mcp@victim.service"

    def _delete_during_lock(name: str):
        # Stand-in for a concurrent delete_project_handler landing while
        # we hold the lock: drop the registry row mid-critical-section.
        router_module._REGISTRY.unregister(name)
        return None

    monkeypatch.setattr(
        _po, "ensure_forwarding_hmac_key", _delete_during_lock,
    )

    with pytest.raises(web.HTTPNotFound):
        await _po._ensure("victim", "backend")

    assert not systemctl_stub.counts.get(("start", unit)), (
        "must NOT systemctl-start a project deleted while _ensure held "
        "the lock (TOCTOU orphan)"
    )
    assert not systemctl_stub.counts.get(("restart", unit))


# ── BL-R6-2b: systemctl runs off the event loop ────────────────────


async def test_systemctl_start_runs_off_event_loop(
    router_module, register_project, monkeypatch,
) -> None:
    """A slow ``systemctl start`` MUST NOT block the event loop.

    ``_systemctl`` is a synchronous ``subprocess.run``; the fix runs it
    via ``asyncio.to_thread`` so a concurrent coroutine keeps making
    progress while the shell-out blocks. We block the stubbed
    ``systemctl start`` in a worker thread and assert a sibling
    coroutine observes the loop as free almost immediately. Against
    #319 (direct call on the loop) the sibling only runs after the full
    block elapses.

    The block is self-releasing (bounded ``time.sleep``) so a
    regression fails on the timing assertion rather than hanging the
    suite.
    """
    from agent_mcp.router import project_orchestrator as _po

    register_project("slow")
    started = threading.Event()
    started_at: list[float] = []
    BLOCK_SEC = 0.4

    def _blocking_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb in ("start", "restart"):
            started_at.append(time.monotonic())
            started.set()
            time.sleep(BLOCK_SEC)  # bounded: no permanent hang on regress
            return subprocess.CompletedProcess(list(args), 0, "", "")
        if verb == "is-active":
            # Report inactive so _ensure decides needs_start → start.
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(_po, "_systemctl", _blocking_systemctl)
    monkeypatch.setattr(router_module, "_systemctl", _blocking_systemctl)

    loop_free_at: list[float] = []

    async def _probe() -> None:
        while not started.is_set():
            await asyncio.sleep(0.005)
        loop_free_at.append(time.monotonic())

    warm = asyncio.create_task(_po._ensure("slow", "backend"))
    probe = asyncio.create_task(_probe())

    # While systemctl blocks in its worker thread, the loop must stay free —
    # the probe records loop_free_at ~immediately after the start began.
    await assert_ran_off_event_loop(
        started_at, loop_free_at, block_sec=BLOCK_SEC,
        what="systemctl start",
    )

    for task in (warm, probe):
        with contextlib.suppress(Exception):
            await task


# ── BL-R6-2a: dedup — rapid GETs schedule at most one warm task ─────


async def test_two_rapid_app_gets_schedule_at_most_one_warm(
    aiohttp_client, router_module, router_app, register_project,
    write_dashboard_file, monkeypatch,
) -> None:
    """Two rapid ``GET /app/<name>/`` requests schedule at most ONE
    warm-start task.

    #319 fired one untracked ``asyncio.create_task`` per GET; a shell-
    only GET flood accumulated redundant tasks. The dedup keeps a single
    warm-start pending per project. RED against #319 (two tasks → two
    ``_ensure`` calls).
    """
    write_dashboard_file("index.html", "<!doctype html><html></html>")
    register_project("dedup-me")  # sentinel authorized (auto-login)

    calls: list[str] = []
    gate = asyncio.Event()

    async def _slow_ensure(name: str, role: str):
        calls.append(name)
        await gate.wait()  # keep the first task pending across both GETs
        return None

    monkeypatch.setattr(router_module, "_ensure", _slow_ensure)
    client = await aiohttp_client(router_app)

    r1 = await client.get("/agent-mcp/app/dedup-me/")
    r2 = await client.get("/agent-mcp/app/dedup-me/")
    assert r1.status == 200
    assert r2.status == 200

    # Let any scheduled warm task start before asserting.
    await asyncio.sleep(0.05)
    assert len(calls) == 1, (
        f"expected exactly one warm-start for two rapid GETs, got "
        f"{len(calls)} — the /app/ handler must dedup pending warms"
    )

    gate.set()
    await asyncio.sleep(0.05)
