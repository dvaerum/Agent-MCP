"""R9-F3 (HIGH) — class-sweep gap R8-F3 (PR #674) missed: the SAME
split-invariant post-yield TOCTOU it fixed on ``admin_api.py``'s
project-lifecycle handlers (rename/delete/stop) was never applied to
its two siblings in ``admin_users_api.py`` —
``add_project_membership_handler`` and
``change_project_membership_role_handler``.

Both gate on the SAME two entry-time invariants R8-F3 documented:

  * ``_deny_cross_tenant_project_read(req, name)`` — the MEMBERSHIP
    check: did the caller resolve a role on THIS project? Runs once,
    BEFORE the handler's genuine yield point (``await _json_body(req)``).
  * ``system.projects.manage`` — a coarse deployment-wide CAPABILITY,
    resolved once at route entry by the middleware and (correctly)
    re-checked post-yield via ``perm_gates.revalidate_capability_or_403``.

The membership half is never re-invoked after the yield, so a delegate
whose OWN project membership is revoked mid-flight (capability
untouched) still completes an add/upgrade of a THIRD PARTY's
membership on a project they are no longer a member of.

Fix: route both handlers through the same
``admin_api._revalidate_capability_and_membership_or_403`` helper
R8-F3 built for exactly this recurrence class, instead of the bare
``revalidate_capability_or_403``.
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


# ── Helpers (mirror test_sec_r8f3_project_membership_toctou.py) ─────


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
    group_id = _seed_group("g-delegated-r9f3", "Delegated Admins R9F3")
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


def _patch_paused_json_body(monkeypatch, *, method: str, project: str):
    """Pause ``admin_users_api._json_body`` — the genuine yield point in
    both handlers under test — for a request matching ``method`` against
    ``project``. Returns the two events the test drives."""
    from agent_mcp.router import admin_users_api

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    original_json_body = admin_users_api._json_body

    async def paused_json_body(req):
        if (
            req.match_info.get("name") == project
            and req.method == method
        ):
            body_read_started.set()
            await release_body_read.wait()
        return await original_json_body(req)

    monkeypatch.setattr(admin_users_api, "_json_body", paused_json_body)
    return body_read_started, release_body_read


# ── Test A: add_project_membership_handler slow-drip membership-strip ──


async def test_slow_drip_add_membership_rejects_stale_membership(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """A slow-drip POST granting a THIRD PARTY membership must re-check
    the caller's LIVE project membership after the body-read yield
    point, not just the entry-time snapshot. Capability is left intact
    throughout — only membership is stripped mid-flight. Pre-fix this
    201s despite the mid-flight strip; post-fix it must 404 (the SAME
    uniform not-found ``_deny_cross_tenant_project_read`` gives a
    genuine non-member) and mallory must NOT have been granted
    membership.
    """
    target_project = "race-add-membership"
    register_project(target_project)

    root_id = _seed_user("root-r9f3a", is_sysadmin=True)
    assert root_id
    mallory_id = _seed_user("mallory-r9f3a", is_sysadmin=False)
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r9f3a")

    body_read_started, release_body_read = _patch_paused_json_body(
        monkeypatch, method="POST", project=target_project,
    )

    async def _attack():
        return await client.post(
            f"/agent-mcp/api/router/projects/{target_project}/memberships",
            data=json.dumps({"user_id": mallory_id, "role": "operator"}),
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

    from agent_mcp.router import identity

    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_membership "
            "WHERE project_name = ? AND user_id = ?",
            (target_project, mallory_id),
        ).fetchone()
    assert row is None, (
        "mallory must NOT have been granted membership off alice's "
        "stale, pre-revocation MEMBERSHIP snapshot"
    )

    # Control check: an INDEPENDENT non-racing request from the now-
    # stripped session must also 404 (proves the revocation is real).
    control = await client.post(
        f"/agent-mcp/api/router/projects/{target_project}/memberships",
        data=json.dumps({"user_id": mallory_id, "role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test B: change_project_membership_role_handler slow-drip strip ─────


async def test_slow_drip_change_role_rejects_stale_membership(
    aiohttp_client, router_app, register_project, monkeypatch,
) -> None:
    """Same race, sibling handler: a slow-drip PATCH upgrading a THIRD
    PARTY's role must re-check the caller's LIVE project membership
    after the body-read yield point. Pre-fix this 200s despite the
    mid-flight strip; post-fix it must 404 and mallory's role must be
    unchanged.
    """
    target_project = "race-change-role-membership"
    register_project(target_project)

    root_id = _seed_user("root-r9f3b", is_sysadmin=True)
    assert root_id
    mallory_id = _seed_user("mallory-r9f3b", is_sysadmin=False)
    _seed_project_membership(target_project, user_id=mallory_id, role="viewer")
    client, alice_cookie, alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, target_project, "system.projects.manage",
    )
    admin_client = await aiohttp_client(router_app)
    root_cookie = await _login(admin_client, "root-r9f3b")

    body_read_started, release_body_read = _patch_paused_json_body(
        monkeypatch, method="PATCH", project=target_project,
    )

    async def _attack():
        return await client.patch(
            f"/agent-mcp/api/router/projects/{target_project}/memberships/u:{mallory_id}",
            data=json.dumps({"role": "operator"}),
            headers=_REST_HEADERS,
            cookies={"agent_mcp_session": alice_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    revoke_resp = await _revoke_membership_via_api(
        admin_client, root_cookie, target_project, alice_id,
    )
    assert revoke_resp.status == 200, await revoke_resp.text()

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    from agent_mcp.router import identity

    with identity._connect() as conn:
        row = conn.execute(
            "SELECT role FROM project_membership "
            "WHERE project_name = ? AND user_id = ?",
            (target_project, mallory_id),
        ).fetchone()
    assert row is not None
    assert row["role"] == "viewer", (
        "mallory's role must NOT have been upgraded off alice's stale, "
        "pre-revocation MEMBERSHIP snapshot"
    )

    # Control check: an INDEPENDENT non-racing request from the now-
    # stripped session must also 404 (proves the revocation is real).
    control = await client.patch(
        f"/agent-mcp/api/router/projects/{target_project}/memberships/u:{mallory_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert control.status == 404, await control.text()


# ── Test C: happy-path regression — no false-positive 404s ──────────


async def test_non_racing_member_add_and_change_role_still_succeed(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: normal (non-racing) add/change-role requests from a
    caller whose membership is NOT stripped mid-flight must still
    succeed exactly as before the fix."""
    add_project = "stable-add-membership"
    register_project(add_project)
    mallory_id = _seed_user("mallory-r9f3c", is_sysadmin=False)
    client, cookie, _alice_id, _group_id = await _delegated_member_client(
        aiohttp_client, router_app, add_project, "system.projects.manage",
    )

    add_resp = await client.post(
        f"/agent-mcp/api/router/projects/{add_project}/memberships",
        data=json.dumps({"user_id": mallory_id, "role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert add_resp.status == 201, await add_resp.text()

    change_resp = await client.patch(
        f"/agent-mcp/api/router/projects/{add_project}/memberships/u:{mallory_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert change_resp.status == 200, await change_resp.text()
