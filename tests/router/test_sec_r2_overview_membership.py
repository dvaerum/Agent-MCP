"""SEC round-2 FINDING 4 [MED] — cross-tenant overview/list disclosure.

Owner-authorised defensive review (2026-07-09). ``GET
/api/router/overview`` and ``GET /api/router/projects`` were session-only
with NO membership filter, so any authenticated operator — even a
non-member, non-sysadmin — enumerated EVERY tenant's project names /
stats / aliases. Both listings now filter to the caller's
``project_membership`` (direct or via a group); a sysadmin sees all.
Same class as #283 (which gated the ``{name}`` routes).
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.no_auth_seed_session]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── Helpers (mirror test_sec_router_admin_authz) ───────────────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, *, is_sysadmin: bool = False) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    if is_empty and username != "__test_first_sysadmin":
        # The first-ever user becomes sysadmin implicitly; burn a
        # sentinel so the user under test gets the role we asked for.
        identity.create_user(
            username="__test_first_sysadmin",
            password="ignoredsentinelpassword",
        )
    user_id = identity.create_user(username=username, password="passwordpassword")
    if is_sysadmin:
        with identity._connect() as conn:
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
    return user_id


def _add_membership(user_id: str, project_name: str, *, role: str = "operator") -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
        )


async def _login(client, username: str) -> str:
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": "passwordpassword"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie
    _, _, value = set_cookie.split(";", 1)[0].partition("=")
    return value.strip()


async def _overview_names(client, cookie) -> set[str]:
    resp = await client.get(
        "/agent-mcp/api/router/overview",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    return {p["name"] for p in body["projects"]}


async def _list_names(client, cookie) -> list[str]:
    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    return (await resp.json())["projects"]


# ── overview ────────────────────────────────────────────────────────


async def test_overview_filtered_to_member_projects(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    uid = _seed_user("mallory")
    _add_membership(uid, "alpha", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory")

    assert await _overview_names(client, cookie) == {"alpha"}


async def test_overview_non_member_operator_sees_nothing(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    _seed_user("nobody")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "nobody")

    assert await _overview_names(client, cookie) == set()


async def test_overview_sysadmin_sees_all(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    _seed_user("root", is_sysadmin=True)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    assert await _overview_names(client, cookie) == {"alpha", "beta"}


# ── projects list ───────────────────────────────────────────────────


async def test_list_projects_filtered_to_member(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    uid = _seed_user("mallory")
    _add_membership(uid, "alpha")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory")

    assert await _list_names(client, cookie) == ["alpha"]


async def test_list_projects_sysadmin_sees_all(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    register_project("beta")
    _seed_user("root", is_sysadmin=True)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    assert await _list_names(client, cookie) == ["alpha", "beta"]
