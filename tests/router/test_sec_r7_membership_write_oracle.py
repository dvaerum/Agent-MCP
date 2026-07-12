"""Security round 7 (pentest R7-F1): project-existence ORACLE via the
membership-WRITE routes — a missed sibling of the R6-F2 per-project-route
oracle class.

The three per-project membership WRITE handlers in ``admin_users_api.py`` —

    POST   /api/router/projects/<name>/memberships          (add)
    PATCH  /api/router/projects/<name>/memberships/<mid>     (change role)
    DELETE /api/router/projects/<name>/memberships/<mid>     (remove)

— gated ``_project_exists`` → 404 FIRST, then ``_membership_grant_denied`` →
**403** for a non-member. So a non-sysadmin delegate holding
``system.projects.manage`` but with ZERO membership on project P got a
DIFFERENT response for an existing-but-hidden project (403, whose body leaks
the project name + the caller's own held role) vs a nonexistent project
(404) — a deployment-wide project-existence ORACLE.

R3-F1 (#468) closed only the membership LIST route; R6-F2 (#478) swept the
per-project lifecycle routes in ``admin_api.py`` through
``_deny_cross_tenant_project_read`` (uniform 404, sysadmin-admit before the
existence probe). These three WRITE routes were never swept.

Fix (mirror R6-F2 exactly): route the three write handlers through the shared
``_deny_cross_tenant_project_read`` so ordering is sysadmin-admit → a
non-member gets the SAME 404 ``unknown_project`` for an existing-hidden
project as for a nonexistent one. ``_membership_grant_denied``'s 403 stays
reachable only by an actual MEMBER (the role-rank check: you can't confer a
role above your own) — never by a non-member (indistinguishable from "project
doesn't exist").

Blast radius is ORACLE ONLY — the mutation itself was always blocked before
any DB write (403 or 404 both deny; no cross-tenant grant/revoke ever
succeeds). These tests drive the real middleware + route stack, so the seam
asserted is the wired-up auth/handler seam, not an in-process helper.
"""

from __future__ import annotations

import json

import pytest


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

_PROJ = "proj-hidden"
_BOGUS = "no-such-slug-xyz"


# ── Helpers (mirror test_sec_r3_membership_list_scoping.py) ─────────


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
    user_id: str | None = None,
    group_id: str | None = None,
    role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        if user_id is not None:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, role) VALUES (?, ?, ?)",
                (project, user_id, role),
            )
        else:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, group_id, role) "
                "VALUES (?, NULL, ?, ?)",
                (project, group_id, role),
            )


async def _login(
    client, username: str, password: str = "passwordpassword",
) -> str:
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
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a group
    capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


def _assert_uniform_not_found(status: int, body: dict) -> None:
    """The uniform 404 a non-member must see — no oracle differential, no
    grant-denied / role-rank leak."""
    assert status == 404
    assert body["success"] is False
    assert body["error"] == "not_found"
    # The 403 grant-denied body leaked the caller's held role + "may not
    # confer …"; the uniform 404 must not.
    blob = json.dumps(body)
    assert "may not confer" not in blob
    assert "forbidden" not in blob


# ── R7-F1: the oracle — non-member existing-hidden vs nonexistent ──


async def test_post_nonmember_existing_indistinguishable_from_nonexistent(
    aiohttp_client, router_app, register_project,
) -> None:
    """POST membership: a non-member's response for an EXISTING (hidden)
    project must be the SAME uniform 404 as for a NONEXISTENT slug — no
    403-vs-404 existence oracle, no project-name / held-role leak."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    existing = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": alice_id, "role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    bogus = await client.post(
        f"/agent-mcp/api/router/projects/{_BOGUS}/memberships",
        data=json.dumps({"user_id": alice_id, "role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    existing_body = await existing.json()
    bogus_body = await bogus.json()
    _assert_uniform_not_found(existing.status, existing_body)
    _assert_uniform_not_found(bogus.status, bogus_body)
    assert existing.status == bogus.status
    assert existing_body["error"] == bogus_body["error"]


async def test_patch_nonmember_existing_indistinguishable_from_nonexistent(
    aiohttp_client, router_app, register_project,
) -> None:
    """PATCH membership role: same uniform-404 oracle closure for a
    non-member on an existing-hidden vs nonexistent project."""
    register_project(_PROJ)
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    existing = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    bogus = await client.patch(
        f"/agent-mcp/api/router/projects/{_BOGUS}/memberships/u:{victim_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    existing_body = await existing.json()
    bogus_body = await bogus.json()
    _assert_uniform_not_found(existing.status, existing_body)
    _assert_uniform_not_found(bogus.status, bogus_body)
    assert existing.status == bogus.status


async def test_delete_nonmember_existing_indistinguishable_from_nonexistent(
    aiohttp_client, router_app, register_project,
) -> None:
    """DELETE membership: same uniform-404 oracle closure. The existing
    project has a real membership row (the pre-fix 403 grant-denied path);
    the non-member must not be able to tell it apart from a nonexistent
    slug."""
    register_project(_PROJ)
    victim_id = _seed_user("victim", is_sysadmin=False)
    _seed_project_membership(_PROJ, user_id=victim_id, role="operator")
    client, cookie, _alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    existing = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    bogus = await client.delete(
        f"/agent-mcp/api/router/projects/{_BOGUS}/memberships/u:{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    existing_body = await existing.json()
    bogus_body = await bogus.json()
    _assert_uniform_not_found(existing.status, existing_body)
    _assert_uniform_not_found(bogus.status, bogus_body)
    assert existing.status == bogus.status
    # Mutation was blocked — the victim row survives.
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_membership "
            "WHERE project_name = ? AND user_id = ?",
            (_PROJ, victim_id),
        ).fetchone()
    assert row is not None


# ── Sysadmin keeps full write behaviour on all three ───────────────


async def test_sysadmin_can_add_change_delete_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A sysadmin may add / change / delete any membership — the oracle
    guard must not shadow the legitimate sysadmin path."""
    register_project(_PROJ)
    _seed_user("root", is_sysadmin=True)
    carol_id = _seed_user("carol", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    add = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": carol_id, "role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert add.status == 201, await add.text()

    change = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{carol_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert change.status == 200, await change.text()

    delete = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{carol_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert delete.status == 200, await delete.text()


# ── Operator MEMBER keeps full write behaviour (not over-restricted) ─


async def test_operator_member_can_add_change_delete_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    """A non-sysadmin delegate who IS an operator member of the project may
    still add / change / delete memberships at or below their own role — the
    oracle guard must admit members, and ``_membership_grant_denied`` must
    only 403 an over-reach, never a member's in-scope write."""
    register_project(_PROJ)
    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_PROJ, user_id=alice_id, role="operator")
    bob_id = _seed_user("bob", is_sysadmin=False)

    add = await client.post(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships",
        data=json.dumps({"user_id": bob_id, "role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert add.status == 201, await add.text()

    change = await client.patch(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{bob_id}",
        data=json.dumps({"role": "operator"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert change.status == 200, await change.text()

    delete = await client.delete(
        f"/agent-mcp/api/router/projects/{_PROJ}/memberships/u:{bob_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert delete.status == 200, await delete.text()
