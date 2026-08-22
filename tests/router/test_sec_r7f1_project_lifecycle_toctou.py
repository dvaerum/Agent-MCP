"""R7-F1 (HIGH, live-exploited) — class-sweep miss of R6-F2's
stale-Principal TOCTOU fix on FOUR sibling handlers in ``admin_api.py``.

R6-F2 (merged @3c44953) added ``perm_gates.revalidate_capability_or_403``
after the body-read yield point in all 8 body-bearing handlers of
``admin_users_api.py``. Four handlers under the IDENTICAL pattern in
``admin_api.py`` were missed — the first two found by the pentest agent,
the other two found by a second, independent pentest lane during the
same round and folded into this same fix/PR:

  * ``create_project_handler`` — body read via ``_app._parse_json_body``.
  * ``rename_project_handler`` — same body-read helper.
  * ``delete_project_handler`` — no body read, but a genuine yield point
    via ``async with _ensure_lock(...)`` + the awaited destructive
    ``asyncio.to_thread(_systemctl, "stop", ...)`` before the
    unregister/rmtree/DB-purge sequence.
  * ``stop_project_handler`` — same lock + awaited ``asyncio.to_thread``
    yield-point shape, before the destructive stop.

All four are gated by ``require_capability("system.projects.manage")``
at route-entry, which resolves ``request['principal']`` ONCE before the
handler's genuine yield point. A caller who paces body delivery (create/
rename) or who lands inside a contended ``_ensure_lock`` (delete/stop)
can hold that yield point open while a concurrent request revokes their
group-delegated capability — the paused handler then resumes and
completes its write/destructive-op against the PRE-revocation snapshot
cached at entry.

Live-exploited repro (pentest-loop R7-F1): a non-sysadmin delegate
holding ``system.projects.manage`` via a group opens a slow-drip
``POST /api/router/projects`` (create) or
``PATCH /api/router/projects/<name>`` (rename); mid-pause, a sysadmin
revokes the group's capability grant (commits); the paused request then
completes its body read and resumes → pre-fix 201/200 (mutation
succeeds off the stale snapshot); post-fix must 403. The delete/stop
variant lands a concurrent request inside the SAME per-(name,"backend")
``_ensure_lock`` the handler contends on, revokes mid-hold, then
releases — pre-fix the destructive op still runs off the stale
snapshot; post-fix must 403.

Fix: mirror R6-F2 exactly — call
``perm_gates.revalidate_capability_or_403(req, "system.projects.manage")``
immediately after the body-read line in create/rename, and immediately
alongside the existing inside-lock ``active_conns`` re-check (R3-F3) in
delete/stop, before anything downstream trusts ``req['principal']`` /
``req['is_sysadmin']`` or before the destructive op runs.

The tests below reproduce both race SHAPES deterministically — no real
sleeps. create/rename: a monkeypatched ``_parse_json_body`` pauses
mid-read on an ``asyncio.Event``. delete/stop: the test pre-acquires the
per-project ``_ensure_lock`` itself (mirrors ``test_sec_r3f3_active_conns_toctou.py``'s
``_wait_until_contended`` idiom) so the attack task deterministically
blocks on lock acquisition — no wall-clock timing either way. A
concurrent capability-revocation commits while paused/blocked; the
pause is then released/the lock returned and the handler resumes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_sec_r6_lifecycle_scoping.py /
#    test_sec_r6f2_stale_principal_toctou.py) ───────────────────────


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
    """Create a user. The first-ever user is auto-promoted to sysadmin
    by the router bootstrap, so seed a throwaway sentinel sysadmin
    first when the table is empty to keep the real test user at
    ``is_sysadmin=0`` by default."""
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
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
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


async def _delegated_client(aiohttp_client, router_app, *caps: str):
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a
    group capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated-r7f1", "Delegated Admins R7F1")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


async def _revoke_capabilities_via_api(admin_client, sysadmin_cookie, group_id):
    """Sysadmin revokes ALL capabilities off ``group_id`` via the real
    REST endpoint (mirrors the live pentest repro's mid-race
    revocation), returning the response for the caller to assert on."""
    return await admin_client.put(
        f"/agent-mcp/api/router/groups/{group_id}/capabilities",
        data=json.dumps({"capabilities": []}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": sysadmin_cookie},
        allow_redirects=False,
    )


# ── Test A: create_project_handler slow-drip TOCTOU ─────────────────


@pytest.mark.no_auth_seed_session
async def test_slow_drip_create_project_rejects_stale_capability(
    aiohttp_client, router_app, monkeypatch,
) -> None:
    """A slow-drip POST creating a new project must re-check the
    caller's LIVE capability after the body-read yield point, not the
    entry-time snapshot. Pre-fix this 201s despite the mid-flight
    revocation; post-fix it must 403 and the project must not exist.
    """
    from agent_mcp.router import admin_api
    from agent_mcp.router import app as router_app_module

    root_id = _seed_user("root-r7f1a", is_sysadmin=True)
    assert root_id
    client, alice_cookie, _alice_id, group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r7f1a")

    target_project = "race-create-project"

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_parse_json_body = router_app_module._parse_json_body

    async def paused_parse_json_body(req):
        if (
            req.path == "/agent-mcp/api/router/projects"
            and req.method == "POST"
        ):
            body_read_started.set()
            await release_body_read.wait()
        return await original_parse_json_body(req)

    monkeypatch.setattr(
        router_app_module, "_parse_json_body", paused_parse_json_body,
    )

    async def _attack():
        return await client.post(
            "/agent-mcp/api/router/projects",
            data=json.dumps({"name": target_project}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    # Concurrent LEGITIMATE revocation commits to the DB while the
    # attack's body-read is paused mid-flight.
    revoke_resp = await _revoke_capabilities_via_api(
        admin_client, root_cookie, group_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"

    assert admin_api.__name__  # keep import referenced (module used for docs)
    assert router_app_module._REGISTRY.get(target_project) is None, (
        "project must NOT have been created off alice's stale, "
        "pre-revocation Principal snapshot"
    )

    # Control check: an INDEPENDENT non-racing request from the now-
    # revoked session must also 403 (proves the revocation is real).
    control = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "control-project-a"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 403, await control.text()


# ── Test B: rename_project_handler slow-drip TOCTOU ─────────────────


@pytest.mark.no_auth_seed_session
async def test_slow_drip_rename_project_rejects_stale_capability(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """Same race, sibling handler: a slow-drip PATCH renaming a project
    must re-check the caller's LIVE capability after the body-read
    yield point. Pre-fix this 200s despite the mid-flight revocation;
    post-fix it must 403 and the project must keep its original name.
    """
    from agent_mcp.router import app as router_app_module

    target_project = "race-rename-project"
    register_project(target_project)

    root_id = _seed_user("root-r7f1b", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    # Alice needs project MEMBERSHIP too — ``rename_project_handler``
    # runs ``_deny_cross_tenant_project_read`` BEFORE the body read
    # (path-param only, unaffected by this fix); without membership
    # she'd 404 there and never reach the raced body-read at all.
    _seed_project_membership(target_project, user_id=alice_id, role="operator")
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r7f1b")

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_parse_json_body = router_app_module._parse_json_body

    async def paused_parse_json_body(req):
        if req.match_info.get("name") == target_project and req.method == "PATCH":
            body_read_started.set()
            await release_body_read.wait()
        return await original_parse_json_body(req)

    monkeypatch.setattr(
        router_app_module, "_parse_json_body", paused_parse_json_body,
    )

    async def _attack():
        return await client.patch(
            f"/agent-mcp/api/router/projects/{target_project}",
            data=json.dumps({"name": "renamed-race-project", "grace_days": 7}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    revoke_resp = await _revoke_capabilities_via_api(
        admin_client, root_cookie, group_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"

    assert router_app_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been renamed off alice's stale, "
        "pre-revocation Principal snapshot"
    )
    assert router_app_module._REGISTRY.get("renamed-race-project") is None

    # Control check: an INDEPENDENT non-racing request from the now-
    # revoked session must also 403 (proves the revocation is real).
    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps({"name": "renamed-race-project-2", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 403, await control.text()


# ── Test C: happy-path regression — no false-positive 403s ─────────


@pytest.mark.no_auth_seed_session
async def test_non_racing_delegate_create_and_rename_still_succeed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: a normal (non-racing) request from a caller whose
    capability is NOT revoked mid-flight must still succeed exactly as
    before the fix — the revalidation must not spuriously reject a
    legitimate, uncontested delegate."""
    client, cookie, alice_id, _group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    create_resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "stable-create-project"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert create_resp.status == 201, await create_resp.text()

    target_project = "stable-rename-project"
    register_project(target_project)
    _seed_project_membership(target_project, user_id=alice_id, role="operator")

    rename_resp = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps({"name": "stable-rename-project-renamed", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()


# ── Test D: re-verified class-sweep — helper invoked at both sites ──


async def test_revalidate_helper_invoked_at_all_four_sites(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """Coverage + non-regression: ``create_project_handler``,
    ``rename_project_handler``, ``delete_project_handler`` and
    ``stop_project_handler`` all call ``revalidate_capability_or_403``
    (directly, or transitively via
    ``_revalidate_capability_and_membership_or_403``, which calls it
    first), with the SAME capability string their ``require_capability``
    route gate uses.

    R13-F1: ``rename_project_handler`` now revalidates at BOTH of its
    genuine yield points (body-read + ``_ensure_lock`` acquisition), so
    it contributes TWO entries here, not one; the other three handlers
    are unaffected."""
    from agent_mcp.router import perm_gates

    calls: list[str] = []
    original = perm_gates.revalidate_capability_or_403

    async def spy(req, cap):
        calls.append(cap)
        return await original(req, cap)

    monkeypatch.setattr(perm_gates, "revalidate_capability_or_403", spy)

    client = await aiohttp_client(router_app)

    create_resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "spy-create-project"}),
        headers=_REST_HEADERS,
    )
    assert create_resp.status == 201, await create_resp.text()

    register_project("spy-rename-project")
    rename_resp = await client.patch(
        "/agent-mcp/api/router/projects/spy-rename-project",
        data=json.dumps({"name": "spy-rename-project-renamed", "grace_days": 7}),
        headers=_REST_HEADERS,
    )
    assert rename_resp.status == 200, await rename_resp.text()

    register_project("spy-stop-project")
    stop_resp = await client.post(
        "/agent-mcp/api/router/projects/spy-stop-project/stop",
        data="{}",
        headers=_REST_HEADERS,
    )
    assert stop_resp.status == 200, await stop_resp.text()

    register_project("spy-delete-project")
    delete_resp = await client.delete(
        "/agent-mcp/api/router/projects/spy-delete-project",
        headers=_REST_HEADERS,
    )
    assert delete_resp.status == 200, await delete_resp.text()

    assert calls == [
        "system.projects.manage",  # create_project_handler
        "system.projects.manage",  # rename_project_handler (body-read)
        "system.projects.manage",  # rename_project_handler (lock, R13-F1)
        "system.projects.manage",  # stop_project_handler
        "system.projects.manage",  # delete_project_handler
    ], calls


# ── Test E: delete_project_handler lock-contention TOCTOU ──────────
#
# No body read — the yield point is the awaited destructive
# ``asyncio.to_thread(_systemctl, ...)`` inside a contended
# ``_ensure_lock``. Mirrors ``test_sec_r3f3_active_conns_toctou.py``'s
# ``_wait_until_contended`` idiom: pre-acquire the lock from the test
# itself so the attack task deterministically blocks trying to acquire
# it — no wall-clock timing.


async def _wait_until_contended(
    lock: asyncio.Lock, *, timeout_iters: int = 2000,
) -> None:
    for _ in range(timeout_iters):
        if lock.locked() and lock._waiters and len(lock._waiters) > 0:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(
        "lock was never contended — handler never reached _ensure_lock"
    )


async def test_slow_drip_delete_project_rejects_stale_capability(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """A caller whose group-delegated capability is revoked WHILE their
    delete request is blocked acquiring the per-project ``_ensure_lock``
    must be re-checked before the destructive unregister/rmtree runs.
    Pre-fix this 200s despite the mid-hold revocation; post-fix it must
    403 and the project must remain registered."""
    target_project = "race-delete-project"
    register_project(target_project)

    root_id = _seed_user("root-r7f1c", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(target_project, user_id=alice_id, role="operator")
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r7f1c")

    lock = router_module._ensure_lock(target_project, "backend")
    await lock.acquire()
    try:
        task = asyncio.ensure_future(
            client.delete(
                f"/agent-mcp/api/router/projects/{target_project}",
                headers=_REST_HEADERS,
                cookies={"agent_mcp_session": alice_cookie},
                allow_redirects=False,
            )
        )
        await _wait_until_contended(lock)

        # Concurrent LEGITIMATE revocation commits while the attack is
        # blocked trying to acquire the lock the test is holding.
        revoke_resp = await _revoke_capabilities_via_api(
            admin_client, root_cookie, group_id,
        )
        assert revoke_resp.status == 200, await revoke_resp.text()
    finally:
        lock.release()

    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been deleted off alice's stale, "
        "pre-revocation Principal snapshot"
    )

    # Control check: an INDEPENDENT non-racing request from the now-
    # revoked session must also 403 (proves the revocation is real).
    control = await client.delete(
        f"/agent-mcp/api/router/projects/{target_project}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 403, await control.text()


# ── Test F: stop_project_handler lock-contention TOCTOU ─────────────


async def test_slow_drip_stop_project_rejects_stale_capability(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Same race, sibling handler: a caller whose group-delegated
    capability is revoked WHILE their stop request is blocked acquiring
    the per-project ``_ensure_lock`` must be re-checked before the
    destructive stop runs. Pre-fix this 200s despite the mid-hold
    revocation; post-fix it must 403."""
    target_project = "race-stop-project"
    register_project(target_project)

    root_id = _seed_user("root-r7f1d", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(target_project, user_id=alice_id, role="operator")
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r7f1d")

    lock = router_module._ensure_lock(target_project, "backend")
    await lock.acquire()
    try:
        task = asyncio.ensure_future(
            client.post(
                f"/agent-mcp/api/router/projects/{target_project}/stop",
                data="{}",
                headers=_REST_HEADERS,
                cookies={"agent_mcp_session": alice_cookie},
                allow_redirects=False,
            )
        )
        await _wait_until_contended(lock)

        revoke_resp = await _revoke_capabilities_via_api(
            admin_client, root_cookie, group_id,
        )
        assert revoke_resp.status == 200, await revoke_resp.text()
    finally:
        lock.release()

    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"

    # Control check: an INDEPENDENT non-racing request from the now-
    # revoked session must also 403 (proves the revocation is real).
    control = await client.post(
        f"/agent-mcp/api/router/projects/{target_project}/stop",
        data="{}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 403, await control.text()


# ── Test G: happy-path regression — delete/stop, no false positives ─


async def test_non_racing_delegate_delete_and_stop_still_succeed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: normal (non-racing) delete/stop requests from a
    caller whose capability is NOT revoked mid-flight must still
    succeed exactly as before the fix."""
    client, cookie, alice_id, _group_id = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    stop_project = "stable-stop-project"
    register_project(stop_project)
    _seed_project_membership(stop_project, user_id=alice_id, role="operator")
    stop_resp = await client.post(
        f"/agent-mcp/api/router/projects/{stop_project}/stop",
        data="{}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert stop_resp.status == 200, await stop_resp.text()

    delete_project = "stable-delete-project"
    register_project(delete_project)
    _seed_project_membership(delete_project, user_id=alice_id, role="operator")
    delete_resp = await client.delete(
        f"/agent-mcp/api/router/projects/{delete_project}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert delete_resp.status == 200, await delete_resp.text()
