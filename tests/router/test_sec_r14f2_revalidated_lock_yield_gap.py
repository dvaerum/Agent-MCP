"""R14-F2 (HIGH, CONFIRMED, live-exploited) — ``perm_gates.revalidated_lock``
revalidates capability/membership exactly ONCE, immediately after lock
acquisition, then ``yield``s. Everything a handler does AFTER that yield
— while still lexically inside the ``async with revalidated_lock(...)``
block, still holding the lock — ran completely un-rechecked. A held
``asyncio.Lock`` only blocks OTHER coroutines racing for the SAME lock;
it does nothing to stop an unrelated capability/membership DELETE (a
totally different request) from committing to the DB while this
coroutine is suspended mid-``await`` inside the "protected" block.

Live repro (this file's Test A): ``rename_project_handler`` awaits
``asyncio.to_thread(_systemctl, "stop", ...)`` INSIDE the
``revalidated_lock`` block, before the destructive
``os.rename``/``_REGISTRY.rename``. A caller granted operator-tier
membership + ``system.projects.manage`` via group capability fires a
PATCH rename; mid the ``systemctl stop`` await (AFTER the lock's own
one-shot entry revalidation already passed), a concurrent request
revokes their membership. Pre-fix the rename still succeeds (the
destructive write trusts the stale, now-revoked, entry-time snapshot);
post-fix it must 404 (membership-revoked shape, same uniform not-found
a genuine non-member sees) and the project must keep its original name.

Test B / C class-sweep the identical shape onto ``delete_project_handler``
and ``stop_project_handler`` — both hold a bare awaited
``asyncio.to_thread`` systemctl call inside their own
``revalidated_lock`` block, before their respective destructive
unregister / orchestrator-state-clear.

Fix: ``perm_gates.revalidate_after`` fuses "await this specific
in-lock yield point" + "revalidate immediately after it resolves" into
ONE call (mirrors ``read_body_and_revalidate``'s existing fusion
idiom, one level in) — every ``asyncio.to_thread`` systemctl/`_is_active`
call inside a ``revalidated_lock`` block now goes through it instead of
being awaited bare, so the destructive write that follows always sits
immediately after a FRESH re-check, not the lock's one-shot entry
snapshot.

Pacing idiom: these races don't use lock CONTENTION (that's R13-F1's
shape, in ``test_sec_r13f1_rename_lock_toctou.py``) — they land the
revocation INSIDE the lock, during the awaited ``systemctl`` call. The
router test suite's ``systemctl_stub``/``router_module`` fixtures
already patch ``project_orchestrator._systemctl`` module-wide (see
``conftest.py``); this file layers a further wrapper on top of that
same seam that blocks on a ``threading.Event`` (the stub runs inside
``asyncio.to_thread``'s real worker thread, so a ``threading.Event``,
not an ``asyncio.Event``, is the correct primitive here) until the test
signals it to proceed — no wall-clock timing either way.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_sec_r13f1_rename_lock_toctou.py) ───────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(
    username: str,
    password: str = "passwordpassword",
    *,
    is_sysadmin: bool = False,
) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = (
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
    if is_empty and username != "__test_first_sysadmin":
        identity.create_user(
            username="__test_first_sysadmin",
            password="ignoredsentinelpassword",
        )
    user_id = identity.create_user(username=username, password=password)
    if is_sysadmin:
        with identity._connect() as conn:
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
    return user_id


def _seed_group(group_id: str, name: str) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-08-21T00:00:00')",
            (group_id, name),
        )
    return group_id


def _grant_capability(group_id: str, *caps: str) -> None:
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _seed_project_membership(
    project: str,
    *,
    user_id: str,
    role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project, user_id, role),
        )


async def _login(client, username: str, password: str = "passwordpassword") -> str:
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


async def _delegated_member_client(
    aiohttp_client, router_app, project: str, *caps: str,
):
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a
    group capability grant AND holds an ``operator`` membership row on
    ``project``. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated-r14f2", "Delegated Admins R14F2")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)
    _seed_project_membership(project, user_id=alice_id, role="operator")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


async def _revoke_membership_via_api(
    admin_client, sysadmin_cookie, project: str, user_id: str,
):
    return await admin_client.delete(
        f"/agent-mcp/api/router/projects/{project}/memberships/u:{user_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": sysadmin_cookie},
        allow_redirects=False,
    )


def _pace_systemctl_stop(monkeypatch, po_module, unit: str):
    """Wrap ``po_module._systemctl`` (already patched to the test suite's
    ``systemctl_stub`` recorder by the ``router_module`` fixture) so a
    ``("stop", unit)`` call blocks on a ``threading.Event`` until the
    test releases it. Runs inside ``asyncio.to_thread``'s real worker
    thread — a plain ``threading.Event`` pair, not asyncio primitives.
    Returns ``(entered, release)``."""
    entered = threading.Event()
    release = threading.Event()
    original = po_module._systemctl

    def paced(*args):
        if len(args) >= 2 and args[0] == "stop" and args[1] == unit:
            entered.set()
            release.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(po_module, "_systemctl", paced)
    return entered, release


async def _wait_until_set(event: threading.Event, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() > deadline:
            raise AssertionError(
                "event was never set — handler never reached the "
                "paced systemctl call"
            )
        await asyncio.sleep(0.001)


# ── Test A: rename's in-lock ``systemctl stop`` await, unrevalidated ──


async def test_slow_drip_rename_systemctl_stop_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """A caller whose project MEMBERSHIP is revoked WHILE their rename
    request is suspended mid-``await`` on the in-lock ``systemctl stop``
    call — AFTER ``revalidated_lock``'s own one-shot entry revalidation
    already passed — must be re-checked again before the destructive
    workspace-move/registry-rename runs. Pre-fix this 200s despite the
    mid-await revocation; post-fix it must 404 and the project must
    keep its original name."""
    from agent_mcp.router import app as router_app_module

    target_project = "race-rename-inlock-stop"
    register_project(target_project)

    root_id = _seed_user("root-r14f2a", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r14f2a")

    from agent_mcp.router import project_orchestrator as po_module

    unit = router_app_module._unit_name(target_project, "backend")
    entered, release = _pace_systemctl_stop(monkeypatch, po_module, unit)

    task = asyncio.ensure_future(
        client.patch(
            f"/agent-mcp/api/router/projects/{target_project}",
            data=json.dumps(
                {"name": "renamed-race-inlock-stop", "grace_days": 7},
            ),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )
    )
    await _wait_until_set(entered)

    revoke_resp = await _revoke_membership_via_api(
        admin_client, root_cookie, target_project, alice_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release.set()
    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been renamed off alice's stale, "
        "pre-revocation membership snapshot"
    )
    assert router_module._REGISTRY.get("renamed-race-inlock-stop") is None

    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps(
            {"name": "renamed-race-inlock-stop-2", "grace_days": 7},
        ),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test B: delete's in-lock ``systemctl stop`` await, unrevalidated ──


async def test_slow_drip_delete_systemctl_stop_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """Same shape, ``delete_project_handler``: membership revoked while
    suspended mid-await on the in-lock ``systemctl stop``. Pre-fix the
    delete still completes; post-fix it must 404 and the project must
    still be registered."""
    from agent_mcp.router import app as router_app_module

    target_project = "race-delete-inlock-stop"
    register_project(target_project)

    root_id = _seed_user("root-r14f2b", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r14f2b")

    from agent_mcp.router import project_orchestrator as po_module

    unit = router_app_module._unit_name(target_project, "backend")
    entered, release = _pace_systemctl_stop(monkeypatch, po_module, unit)

    task = asyncio.ensure_future(
        client.delete(
            f"/agent-mcp/api/router/projects/{target_project}",
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )
    )
    await _wait_until_set(entered)

    revoke_resp = await _revoke_membership_via_api(
        admin_client, root_cookie, target_project, alice_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release.set()
    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been unregistered off alice's stale, "
        "pre-revocation membership snapshot"
    )


# ── Test C: stop's in-lock ``systemctl stop`` await, unrevalidated ───


async def test_slow_drip_stop_systemctl_stop_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project,
    systemctl_stub, monkeypatch,
) -> None:
    """Same shape, ``stop_project_handler``: membership revoked while
    suspended mid-await on the in-lock ``systemctl stop`` (past the
    ``_is_active`` probe, which must return True for the handler to
    reach the paced ``systemctl stop`` call at all). Pre-fix the stop
    still completes; post-fix it must 404."""
    from agent_mcp.router import app as router_app_module

    target_project = "race-stop-inlock-stop"
    register_project(target_project)

    root_id = _seed_user("root-r14f2c", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r14f2c")

    from agent_mcp.router import project_orchestrator as po_module

    unit = router_app_module._unit_name(target_project, "backend")
    systemctl_stub.active_units.add(unit)  # _is_active(unit) must be True
    entered, release = _pace_systemctl_stop(monkeypatch, po_module, unit)

    task = asyncio.ensure_future(
        client.post(
            f"/agent-mcp/api/router/projects/{target_project}/stop",
            data="{}",
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )
    )
    await _wait_until_set(entered)

    revoke_resp = await _revoke_membership_via_api(
        admin_client, root_cookie, target_project, alice_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release.set()
    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False


# ── Test D: happy-path regression — no false-positive 404s ─────────


async def test_non_racing_delegate_rename_delete_stop_still_succeed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: normal (non-racing) requests from a caller whose
    membership/capability is NOT revoked mid-flight must still succeed
    exactly as before the fix, across all three handlers."""
    client, cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, "stable-r14f2-rename", "system.projects.manage",
    )
    register_project("stable-r14f2-rename")

    rename_resp = await client.patch(
        "/agent-mcp/api/router/projects/stable-r14f2-rename",
        data=json.dumps({"name": "stable-r14f2-renamed", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()

    register_project("stable-r14f2-stop")
    _seed_project_membership(
        "stable-r14f2-stop", user_id=alice_id, role="operator",
    )
    stop_resp = await client.post(
        "/agent-mcp/api/router/projects/stable-r14f2-stop/stop",
        data="{}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert stop_resp.status == 200, await stop_resp.text()

    register_project("stable-r14f2-delete")
    _seed_project_membership(
        "stable-r14f2-delete", user_id=alice_id, role="operator",
    )
    delete_resp = await client.delete(
        "/agent-mcp/api/router/projects/stable-r14f2-delete",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert delete_resp.status == 200, await delete_resp.text()
