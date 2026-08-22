"""R13-F1 (HIGH, live-exploited) — ``rename_project_handler`` has TWO
separate genuine async yield points, but OBS-R11-1's revalidation
consolidation (PR #690) only ever wired revalidation to the FIRST one.

Yield point 1: the body-read (``read_body_and_revalidate``, revalidated
since R7-F1 / consolidated by OBS-R11-1).

Yield point 2 (this file): acquiring ``_app._ensure_lock(old_name,
"backend")``, then the awaited ``systemctl stop`` and destructive
workspace/token/registry-rename sequence — completely unrevalidated
before this fix. Lock CONTENTION alone can block for a full backend
cold-boot (~9s, empirically timed live), attacker-triggerable for free
via a plain warm-start ``GET /agent-mcp/app/<name>/`` against the target
project (the warm-start path doesn't trip the ``active_conns`` guard).
``perm_gates.py``'s ``revalidated_lock`` docstring explicitly documented
this as a known, deliberate scope decision during the OBS-R11-1 refactor
— not something the refactor newly introduced, but a real, live-
exploitable gap regardless: a caller whose capability/membership is
revoked AFTER the body-read revalidation but WHILE blocked acquiring
this second lock can still have their rename go through using authority
that no longer exists by the time the destructive write runs.

This mirrors ``test_sec_r7f1_project_lifecycle_toctou.py``'s Test E/F
lock-contention harness (``_wait_until_contended``, pre-acquiring the
per-project ``_ensure_lock`` from the test itself so the attack task
deterministically blocks — no wall-clock timing) and
``test_sec_r8f3_project_membership_toctou.py``'s membership-only
revocation variant, applied to rename's SECOND yield point instead of
its first.

Fix: wrap ``rename_project_handler``'s ``async with
_app._ensure_lock(old_name, "backend"):`` in ``perm_gates.revalidated_lock``
— exactly like ``delete_project_handler`` / ``stop_project_handler``
already do — IN ADDITION to (not instead of) the existing body-read
revalidation.
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


# ── Helpers (mirror test_sec_r7f1_project_lifecycle_toctou.py /
#    test_sec_r8f3_project_membership_toctou.py) ────────────────────


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
    group_id = _seed_group("g-delegated-r13f1", "Delegated Admins R13F1")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)
    _seed_project_membership(project, user_id=alice_id, role="operator")

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


async def _revoke_membership_via_api(
    admin_client, sysadmin_cookie, project: str, user_id: str,
):
    """Sysadmin strips the delegate's membership on ``project`` via the
    real REST endpoint. Capability is left untouched — this is the
    MEMBERSHIP-only half of the two revocation shapes the finding calls
    out ("or group capability")."""
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


# ── Test A: rename's SECOND yield point — capability revoked while
#    blocked acquiring the lock ─────────────────────────────────────


async def test_slow_drip_rename_lock_contention_rejects_stale_capability(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """A caller whose group-delegated capability is revoked WHILE their
    rename request is blocked acquiring the per-project ``_ensure_lock``
    (rename's SECOND, independent yield point — past the body-read
    revalidation) must be re-checked before the destructive
    stop/move/registry-rename runs. Pre-fix this 200s despite the mid-
    hold revocation; post-fix it must 403 and the project must keep its
    original name."""
    target_project = "race-rename-lock"
    register_project(target_project)

    root_id = _seed_user("root-r13f1a", is_sysadmin=True)
    assert root_id
    client, alice_cookie, _alice_id, group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r13f1a")

    lock = router_module._ensure_lock(target_project, "backend")
    await lock.acquire()
    try:
        task = asyncio.ensure_future(
            client.patch(
                f"/agent-mcp/api/router/projects/{target_project}",
                data=json.dumps(
                    {"name": "renamed-race-lock", "grace_days": 7},
                ),
                headers=_REST_HEADERS,
                cookies={"agent_mcp_session": alice_cookie},
                allow_redirects=False,
            )
        )
        # The body-read yield point (rename's FIRST, already-revalidated
        # yield point) has to fully complete BEFORE the handler reaches
        # the lock this test is holding — pacing here proves the race
        # lands on the SECOND yield point, not the first.
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
    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been renamed off alice's stale, "
        "pre-revocation Principal snapshot"
    )
    assert router_module._REGISTRY.get("renamed-race-lock") is None

    # Control check: an INDEPENDENT non-racing request from the now-
    # revoked session must also 403 (proves the revocation is real).
    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps({"name": "renamed-race-lock-2", "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 403, await control.text()


# ── Test B: same shape, MEMBERSHIP-only revocation ──────────────────


async def test_slow_drip_rename_lock_contention_rejects_stale_membership(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Same race as Test A, membership-only half: capability is left
    intact throughout, only the project membership row is stripped
    while the attack is blocked on the lock. Pre-fix this 200s despite
    the mid-hold strip; post-fix it must 404 (the SAME uniform
    not-found a genuine non-member sees)."""
    target_project = "race-rename-lock-membership"
    register_project(target_project)

    root_id = _seed_user("root-r13f1b", is_sysadmin=True)
    assert root_id
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r13f1b")

    lock = router_module._ensure_lock(target_project, "backend")
    await lock.acquire()
    try:
        task = asyncio.ensure_future(
            client.patch(
                f"/agent-mcp/api/router/projects/{target_project}",
                data=json.dumps(
                    {"name": "renamed-race-lock-membership", "grace_days": 7},
                ),
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

    assert router_module._REGISTRY.get(target_project) is not None, (
        "project must NOT have been renamed off alice's stale, "
        "pre-revocation MEMBERSHIP snapshot"
    )
    assert router_module._REGISTRY.get("renamed-race-lock-membership") is None

    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps(
            {"name": "renamed-race-lock-membership-2", "grace_days": 7},
        ),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test C: happy-path regression — no false-positive 403/404s ─────


async def test_non_racing_delegate_rename_still_succeeds(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: a normal (non-racing, non-contended) rename from a
    caller whose capability/membership is NOT revoked mid-flight must
    still succeed exactly as before the fix."""
    target_project = "stable-rename-lock-project"
    register_project(target_project)
    client, cookie, _alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )

    rename_resp = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}",
        data=json.dumps(
            {"name": "stable-rename-lock-project-renamed", "grace_days": 7},
        ),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert rename_resp.status == 200, await rename_resp.text()
