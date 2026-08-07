"""Wave 9 PR 4 — router-side ``require_capability`` gate.

Wave 9 PR 4 of 7 in ``prancy-napping-pie.md``. Pins the new
behaviour of the router-admin route gates after the
``require_sysadmin`` → ``require_capability`` migration:

  * Existing **status quo** holds: sysadmin admits every gated route
    (covered by the pre-Wave-9 ``test_p3_perm_overhaul`` tests, which
    keep passing because sysadmins carry the ``SYSADMIN_WILDCARD``
    cap and the wildcard short-circuits ``has_capability``).
  * **Capability delegation** works: a non-sysadmin operator who has
    been granted ``system.users.manage`` via a group's
    ``group_capability`` row admits the user-CRUD route. This is the
    Wave 9 deliverable — capability-based authorization replaces the
    sysadmin-or-bust binary gate.
  * **Default-deny** holds for non-sysadmin operators WITHOUT the
    relevant cap: hitting the mutation handler returns 403 with the
    new envelope shape (``error="forbidden"`` + ``message`` naming
    the missing cap).

Tests are scenario-shaped (seed users / groups / caps via the
identity + group_capability_repository helpers, log in, hit the
endpoint, assert the status code + envelope). They go end-to-end
through the same middleware stack the production code uses, so the
seam being asserted is the wired-up auth seam, not any in-process
helper.
"""

from __future__ import annotations

import json

import pytest

# Bypass the conftest auto-login: each test seeds its own user(s)
# and logs in explicitly so the assertion targets the real
# sysadmin-vs-cap-delegation distinction rather than a side-effect
# of the sentinel-operator fixture.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror the test_p3_perm_overhaul pattern) ─────────────


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
    """Create a non-sysadmin user (unless ``is_sysadmin``)."""
    identity = _identity_module()
    # The router's first-user-is-sysadmin bootstrap fires on the first
    # ``create_user`` call. Seed a sentinel sysadmin first so the
    # actual test user lands at is_sysadmin=0 by default.
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
    """Create a group with the given id + name. Returns the group_id."""
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )
    return group_id


def _grant_capability(group_id: str, *caps: str) -> None:
    """Grant ``caps`` to ``group_id`` via the group_capability_repository."""
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _add_membership(
    user_id: str, project_name: str, *, role: str = "operator",
) -> None:
    """Grant ``user_id`` membership in ``project_name`` at ``role``."""
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
        )


async def _login(client, username: str, password: str = "passwordpassword") -> str:
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


# ── Delegation: non-sysadmin + cap admits the route ────────────────


async def test_non_sysadmin_with_system_users_manage_can_create_user(
    aiohttp_client, router_app,
) -> None:
    """Wave 9 PR 4: a non-sysadmin operator who has been granted
    ``system.users.manage`` via a group's capability row admits the
    user-CRUD POST route. The pre-Wave-9 gate (require_sysadmin)
    would 403 this caller; the new cap-shaped gate admits because
    the resolved Principal carries the granted cap.
    """
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-user-admins", "User Admins")
    _grant_capability(group_id, "system.users.manage")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # Cap-delegated admit: anything that's NOT 403 proves the gate
    # opened. The exact status (201 success or 409 conflict if
    # newcomer already exists) is the handler's call, not the gate's.
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_non_sysadmin_with_system_groups_manage_can_create_group(
    aiohttp_client, router_app,
) -> None:
    """Same shape as the user-CRUD test, but for ``system.groups.manage``
    on the group-CRUD POST route. Proves the cap-mapping in
    ``admin_users_api.register_admin_users_routes`` is correct
    (group routes use the groups cap, not the users cap)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-group-admins", "Group Admins")
    _grant_capability(group_id, "system.groups.manage")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "engineers"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_non_sysadmin_with_system_projects_manage_can_create_project(
    aiohttp_client, router_app,
) -> None:
    """``system.projects.manage`` delegation admits the project-CRUD
    POST route (admin_api.register_admin_routes)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-proj-admins", "Project Admins")
    _grant_capability(group_id, "system.projects.manage")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "proj-delegated"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_non_sysadmin_with_system_sso_configure_can_read_sso_config(
    aiohttp_client, router_app,
) -> None:
    """``system.sso.configure`` delegation admits the SSO config GET
    route (admin_sso_api.register_admin_sso_routes)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-sso-admins", "SSO Admins")
    _grant_capability(group_id, "system.sso.configure")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.get(
        "/agent-mcp/api/router/sso/config",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # SSO config GET returns 200 with the introspected config when
    # delegated; the gate is what we're asserting on (not the body
    # shape, which test_admin_sso_config_api covers).
    assert resp.status != 403, await resp.text()
    assert resp.status == 200, await resp.text()


# ── Default-deny: non-sysadmin WITHOUT cap is 403 ─────────────────


async def test_non_sysadmin_without_users_cap_cannot_create_user(
    aiohttp_client, router_app,
) -> None:
    """A logged-in non-sysadmin operator who has NOT been granted
    ``system.users.manage`` (no group, no cap) gets 403 with the new
    capability-shaped error envelope.
    """
    _seed_user("alice", is_sysadmin=False)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    # The new envelope's message names the missing capability so
    # log-grep / UX can distinguish "lacks cap X" from a generic
    # "you don't have access" 403.
    assert "system.users.manage" in body["message"]


async def test_viewer_member_without_system_cap_cannot_create_user(
    aiohttp_client, router_app, register_project,
) -> None:
    """A viewer-tier project member (a non-sysadmin operator with
    project_role='viewer' on some project) does NOT carry
    ``system.users.manage`` — the viewer bundle covers reads only.
    Hitting the user-CRUD POST route returns 403.

    Belt-and-braces test: confirms the cap-set resolution chain
    correctly DENIES the system.* cap to a viewer (no group_capability
    rows + viewer bundle has only ``system.view``, not
    ``system.users.manage``).
    """
    register_project("alpha")
    viewer_id = _seed_user("vera", is_sysadmin=False)
    _add_membership(viewer_id, "alpha", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "vera")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"
    assert "system.users.manage" in body["message"]


async def test_users_cap_does_not_admit_groups_route(
    aiohttp_client, router_app,
) -> None:
    """A delegated ``system.users.manage`` cap does NOT admit the
    group-CRUD route — the per-route cap mapping is enforced
    (Wave 9 PR 4 ships one cap per route family, not a single
    "system.manage" wildcard)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _create_group("g-user-admins", "User Admins")
    _grant_capability(group_id, "system.users.manage")
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "engineers"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert "system.groups.manage" in body["message"]


# ── Sysadmin keeps admitting (regression guard) ────────────────────


async def test_sysadmin_still_admits_user_crud_post_wave9(
    aiohttp_client, router_app,
) -> None:
    """Regression guard: a sysadmin keeps admitting the user-CRUD
    POST route after the gate moved to ``require_capability``. The
    Wave 9 PR 0 ``SYSADMIN_WILDCARD`` short-circuit in
    ``Principal.has_capability`` is what makes this work."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newcomer",
            "password": "newcomerpassword",
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    assert resp.status in (201, 400, 409), await resp.text()


async def test_sysadmin_still_admits_sso_config_post_wave9(
    aiohttp_client, router_app,
) -> None:
    """Regression guard for the SSO config route + the
    require_capability sysadmin short-circuit."""
    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")
    resp = await client.get(
        "/agent-mcp/api/router/sso/config",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status != 403, await resp.text()
    # The route returns 200 with the introspected config (mode=builtin
    # when no SSO env vars are set, which is the test default).
    assert resp.status == 200, await resp.text()
