"""Security: router-admin GET reads must be capability-gated.

Owner-authorized defensive review (2026-07-08), FINDING 2 [MED].

The router-admin GET routes ``list_users`` / ``list_groups`` /
``list_project_memberships`` (``router/admin_users_api.py``) were
registered with ``gated(...)`` (the Accept-header + auth gate) but NO
capability gate. Any authenticated caller — including a plain
project *viewer* — could enumerate every user + email + is_sysadmin
flag, every group, and every project membership.

The sibling *mutation* routes are gated on:

  * user CRUD             → ``system.users.manage``
  * group CRUD            → ``system.groups.manage``
  * project-membership CRUD → ``system.projects.manage``

These reads expose the same identity data the mutations manage, so
each GET is gated on the SAME capability as its sibling mutations.
A viewer / non-authorized operator is denied (403); a sysadmin (or an
operator delegated the cap via a group) passes.
"""

from __future__ import annotations

import pytest


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}
_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_wave9_pr4_require_capability) ─────────────


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
        cur = conn.execute("SELECT COUNT(*) AS n FROM users")
        is_empty = cur.fetchone()["n"] == 0
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


def _create_group(group_id: str, name: str) -> str:
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


def _add_membership(
    user_id: str, project_name: str, *, role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
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


# ── Default-deny: viewer-tier member is 403 on every read ──────────


async def test_viewer_denied_list_users(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.get(
        "/agent-mcp/api/router/users",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    assert "system.users.manage" in body["message"]


async def test_viewer_denied_list_groups(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.get(
        "/agent-mcp/api/router/groups",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    assert "system.groups.manage" in body["message"]


async def test_viewer_denied_list_project_memberships(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/memberships",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    assert "system.projects.manage" in body["message"]


# ── Sysadmin admits every read (regression guard) ──────────────────


async def test_sysadmin_admits_list_users(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.get(
        "/agent-mcp/api/router/users",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()


async def test_sysadmin_admits_list_groups(
    aiohttp_client, router_app,
) -> None:
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.get(
        "/agent-mcp/api/router/groups",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()


async def test_sysadmin_admits_list_project_memberships(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/memberships",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()


# ── Cap delegation admits the read (Wave-9 shape) ──────────────────


async def test_delegated_users_cap_admits_list_users(
    aiohttp_client, router_app,
) -> None:
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-user-admins", "User Admins")
    _grant_capability(group_id, "system.users.manage")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.get(
        "/agent-mcp/api/router/users",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status == 200, await resp.text()
