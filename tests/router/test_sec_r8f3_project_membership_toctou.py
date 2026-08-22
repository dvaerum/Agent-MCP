"""R8-F3 (HIGH, live-exploited) — class-sweep gap IN R7-F1's OWN fix
(PR #674): the capability revalidation R7-F1 added post-yield-point
re-checks ONLY the capability half of the entry gate, never the
MEMBERSHIP half.

``rename_project_handler`` / ``delete_project_handler`` /
``stop_project_handler`` each gate on TWO separate entry-time checks:

  * ``_deny_cross_tenant_project_read(req, name)`` — R6-F2's MEMBERSHIP
    check: did the caller resolve a role on THIS project? Runs once,
    BEFORE the handler's genuine yield point (path-param only).
  * ``require_capability("system.projects.manage")`` — a coarse
    deployment-wide CAPABILITY check, resolved once at route entry by
    the middleware.

R7-F1 (#674) added ``perm_gates.revalidate_capability_or_403`` right
after each handler's yield point (a body-read for rename; lock
contention for delete/stop) to re-check the CAPABILITY half against a
live DB read. But that function rebuilds the Principal with
``project_role=None`` BY DESIGN (router-admin routes are never
project-scoped) — it never re-invokes
``_deny_cross_tenant_project_read``. The MEMBERSHIP check therefore
still runs exactly ONCE, using the entry-time snapshot, even after
R7-F1's fix landed.

Live-exploited repro (round-8 fix-regression pentest lane): a delegate
holds ``system.projects.manage`` via a group grant AND is an
``operator`` member of project X. They open a slow-drip PATCH renaming
X; mid-pause, a sysadmin revokes ONLY the delegate's membership on X
(not their capability) via a real ``DELETE
.../memberships/<membership_id>``. The paused body-read resumes →
pre-fix 200 (rename lands off the stale membership snapshot, using
only the residual coarse capability); post-fix must 404 (the SAME
uniform not-found ``_deny_cross_tenant_project_read`` gives a genuine
non-member, so a stripped member can't distinguish "revoked" from
"never existed"). The delete/stop variants mirror this with the
lock-contention yield-point shape ``test_sec_r7f1_project_lifecycle_
toctou.py`` already established for the capability half.

Fix: ``_revalidate_capability_and_membership_or_403`` in
``admin_api.py`` composes BOTH re-checks in one call, so a future
yield-point fix can't repeat this exact split-invariant miss.
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


# ── Helpers (mirror test_sec_r7f1_project_lifecycle_toctou.py) ──────


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


async def _delegated_member_client(
    aiohttp_client, router_app, project: str, *caps: str,
):
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a
    group capability grant AND holds an ``operator`` membership row on
    ``project``. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated-r8f3", "Delegated Admins R8F3")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)
    _seed_project_membership(project, user_id=alice_id, role="operator")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


async def _revoke_membership_via_api(
    admin_client, sysadmin_cookie, project: str, user_id: str,
):
    """Sysadmin strips the delegate's membership on ``project`` via the
    real REST endpoint (mirrors the live pentest repro's mid-race
    revocation), returning the response for the caller to assert on.
    Capability is left untouched — this is the MEMBERSHIP-only half."""
    return await admin_client.delete(
        f"/agent-mcp/api/router/projects/{project}/memberships/u:{user_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": sysadmin_cookie},
        allow_redirects=False,
    )


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


# ── Test A: rename_project_handler slow-drip membership-strip TOCTOU ─


@pytest.mark.no_auth_seed_session
async def test_slow_drip_rename_project_rejects_stale_membership(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """A slow-drip PATCH renaming a project must re-check the caller's
    LIVE project membership after the body-read yield point, not just
    the entry-time snapshot. Capability is left intact throughout —
    only membership is stripped mid-flight. Pre-fix this 200s despite
    the mid-flight strip; post-fix it must 404 (unknown_project — the
    SAME uniform not-found a genuine non-member sees) and the project
    must keep its original name.
    """
    from agent_mcp.router import app as router_app_module

    target_project = "race-rename-membership"
    register_project(target_project)

    root_id = _seed_user("root-r8f3a", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r8f3a")

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
            data=json.dumps({"name": "renamed-race-membership", "grace_days": 7}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    # Concurrent LEGITIMATE membership strip commits to the DB while the
    # attack's body-read is paused mid-flight. Capability is untouched.
    revoke_resp = await _revoke_membership_via_api(
        admin_client, root_cookie, target_project, alice_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    assert router_app_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been renamed off alice's stale, "
        "pre-revocation MEMBERSHIP snapshot"
    )
    assert router_app_module._REGISTRY.get("renamed-race-membership") is None

    # Control check: an INDEPENDENT non-racing request from the now-
    # stripped session must also 404 (proves the revocation is real).
    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps({"name": "renamed-race-membership-2", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test B: delete_project_handler lock-contention membership-strip ─


async def test_slow_drip_delete_project_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """A caller whose project MEMBERSHIP is stripped WHILE their delete
    request is blocked acquiring the per-project ``_ensure_lock`` must
    be re-checked before the destructive unregister/rmtree runs.
    Capability is left intact throughout. Pre-fix this 200s despite the
    mid-hold strip; post-fix it must 404 and the project must remain
    registered."""
    target_project = "race-delete-membership"
    register_project(target_project)

    root_id = _seed_user("root-r8f3b", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r8f3b")

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

        # Concurrent LEGITIMATE membership strip commits while the
        # attack is blocked trying to acquire the lock the test holds.
        revoke_resp = await _revoke_membership_via_api(
            admin_client, root_cookie, target_project, alice_id,
        )
        assert revoke_resp.status == 200, await revoke_resp.text()
    finally:
        lock.release()

    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been deleted off alice's stale, "
        "pre-revocation MEMBERSHIP snapshot"
    )

    # Control check: an INDEPENDENT non-racing request from the now-
    # stripped session must also 404 (proves the revocation is real).
    control = await client.delete(
        f"/agent-mcp/api/router/projects/{target_project}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test C: stop_project_handler lock-contention membership-strip ───


async def test_slow_drip_stop_project_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Same race, sibling handler: a caller whose project MEMBERSHIP is
    stripped WHILE their stop request is blocked acquiring the
    per-project ``_ensure_lock`` must be re-checked before the
    destructive stop runs. Capability is left intact throughout.
    Pre-fix this 200s despite the mid-hold strip; post-fix it must
    404."""
    target_project = "race-stop-membership"
    register_project(target_project)

    root_id = _seed_user("root-r8f3c", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r8f3c")

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

        revoke_resp = await _revoke_membership_via_api(
            admin_client, root_cookie, target_project, alice_id,
        )
        assert revoke_resp.status == 200, await revoke_resp.text()
    finally:
        lock.release()

    resp = await asyncio.wait_for(task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    # Control check: an INDEPENDENT non-racing request from the now-
    # stripped session must also 404 (proves the revocation is real).
    control = await client.post(
        f"/agent-mcp/api/router/projects/{target_project}/stop",
        data="{}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test D: happy-path regression — no false-positive 404s ──────────


async def test_non_racing_member_rename_delete_stop_still_succeed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: normal (non-racing) rename/delete/stop requests from
    a caller whose membership is NOT stripped mid-flight must still
    succeed exactly as before the fix — the membership revalidation
    must not spuriously reject a legitimate, uncontested member."""
    rename_project = "stable-rename-membership"
    register_project(rename_project)
    client, cookie, _alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, rename_project, "system.projects.manage",
    )

    rename_resp = await client.patch(
        f"/agent-mcp/api/router/projects/{rename_project}",
        data=json.dumps(
            {"name": "stable-rename-membership-renamed", "grace_days": 7},
        ),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()

    stop_project = "stable-stop-membership"
    register_project(stop_project)
    _seed_project_membership(stop_project, user_id=_alice_id, role="operator")
    stop_resp = await client.post(
        f"/agent-mcp/api/router/projects/{stop_project}/stop",
        data="{}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert stop_resp.status == 200, await stop_resp.text()

    delete_project = "stable-delete-membership"
    register_project(delete_project)
    _seed_project_membership(delete_project, user_id=_alice_id, role="operator")
    delete_resp = await client.delete(
        f"/agent-mcp/api/router/projects/{delete_project}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert delete_resp.status == 200, await delete_resp.text()


# ── Test E: helper composes both checks at all 3 sites ──────────────


async def test_combined_revalidation_helper_invoked_at_all_three_sites(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """Coverage + non-regression: ``rename_project_handler``,
    ``delete_project_handler`` and ``stop_project_handler`` all call
    ``_revalidate_capability_and_membership_or_403``, with the SAME
    capability string their ``require_capability`` route gate uses, and
    the correct in-flight project name.

    R13-F1: ``rename_project_handler`` now revalidates at BOTH of its
    genuine yield points — the body-read (``read_body_and_revalidate``)
    AND the ``_ensure_lock`` acquisition (``revalidated_lock``) — so it
    calls this helper TWICE, not once.

    R14-F2: the in-lock ``asyncio.to_thread`` systemctl-stop (and, for
    stop, ``_is_active`` too) awaits now each route through
    ``perm_gates.revalidate_after``, which calls THIS SAME helper again
    — a THIRD call for rename, a second for delete, and a second for
    stop (the ``_is_active`` re-check; the project isn't actually
    running in this test, so the further ``systemctl stop``-guarded
    re-check never fires).

    R9-F2: also pins that all now thread ``min_role="operator"``
    through the post-yield re-check, mirroring the entry-time check —
    the rank bar must not silently drop across the yield point."""
    from agent_mcp.router import admin_api

    calls: list[tuple[str, str, str | None]] = []
    original = admin_api._revalidate_capability_and_membership_or_403

    async def spy(req, cap, project_name, *, min_role=None):
        calls.append((cap, project_name, min_role))
        return await original(req, cap, project_name, min_role=min_role)

    monkeypatch.setattr(
        admin_api, "_revalidate_capability_and_membership_or_403", spy,
    )

    client = await aiohttp_client(router_app)

    register_project("spy-rename-membership")
    rename_resp = await client.patch(
        "/agent-mcp/api/router/projects/spy-rename-membership",
        data=json.dumps(
            {"name": "spy-rename-membership-renamed", "grace_days": 7},
        ),
        headers=_REST_HEADERS,
    )
    assert rename_resp.status == 200, await rename_resp.text()

    register_project("spy-stop-membership")
    stop_resp = await client.post(
        "/agent-mcp/api/router/projects/spy-stop-membership/stop",
        data="{}",
        headers=_REST_HEADERS,
    )
    assert stop_resp.status == 200, await stop_resp.text()

    register_project("spy-delete-membership")
    delete_resp = await client.delete(
        "/agent-mcp/api/router/projects/spy-delete-membership",
        headers=_REST_HEADERS,
    )
    assert delete_resp.status == 200, await delete_resp.text()

    assert calls == [
        ("system.projects.manage", "spy-rename-membership", "operator"),
        ("system.projects.manage", "spy-rename-membership", "operator"),
        ("system.projects.manage", "spy-rename-membership", "operator"),
        ("system.projects.manage", "spy-stop-membership", "operator"),
        ("system.projects.manage", "spy-stop-membership", "operator"),
        ("system.projects.manage", "spy-delete-membership", "operator"),
        ("system.projects.manage", "spy-delete-membership", "operator"),
    ], calls
