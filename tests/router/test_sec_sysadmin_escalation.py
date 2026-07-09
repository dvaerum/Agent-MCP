"""Security: sysadmin-grant and group-cycle guards on the router admin API.

Wave-B pentest-loop fixes (owner-authorized, defensive).

Finding 1 [MED-HIGH] — self-escalation to sysadmin via delegated caps.
    ``create``/``edit`` on users and groups let the caller set
    ``is_sysadmin`` gated ONLY by ``system.users.manage`` /
    ``system.groups.manage``. Because a sysadmin-flagged GROUP confers
    sysadmin to its members (``group_resolver.resolve_user_is_sysadmin``
    walks the transitive closure), a delegated operator with
    ``system.groups.manage`` could mint a sysadmin group, add
    themselves, and become full sysadmin. The fix: writing the
    ``is_sysadmin`` bit (granting on create/edit, or clearing on edit)
    requires the CALLER to already be sysadmin — not merely the
    ``system.*.manage`` cap. Granting sysadmin is strictly a
    sysadmin-only operation.

Finding 2 [MED] — missing cycle detection on the production
    ``add_group_member`` handler. The handler did a raw INSERT with no
    cycle check, so an operator could create A∈B and B∈A, violating the
    membership-DAG invariant. The safe ``group_resolver`` cycle logic is
    now wired into the handler in one BEGIN IMMEDIATE transaction
    (check-then-insert atomically).

These tests go end-to-end through the same middleware + route stack the
production code uses, so the seam asserted is the wired-up auth/handler
seam, not an in-process helper.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}
_STRICT_ACCEPT = _REST_HEADERS


# ── Helpers (mirror the test_wave9_pr4_require_capability pattern) ──


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
        is_empty = conn.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone()["n"] == 0
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


def _grant_capability(group_id: str, *caps: str) -> None:
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _seed_group(group_id: str, name: str) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )
    return group_id


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
    group capability grant. Returns (client, cookie)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie


# ── Finding 1: non-sysadmin CANNOT grant sysadmin ──────────────────


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_cannot_create_sysadmin_user(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin operator with ``system.users.manage`` cannot mint
    a sysadmin-flagged USER — the escalation vector is denied 403."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "backdoor",
            "password": "backdoorpassword",
            "is_sysadmin": True,
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_cannot_edit_user_to_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin operator with ``system.users.manage`` cannot
    PATCH an existing user up to sysadmin."""
    victim_id = _seed_user("victim", is_sysadmin=False)
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.patch(
        f"/agent-mcp/api/router/users/{victim_id}",
        data=json.dumps({"is_sysadmin": True}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"


@pytest.mark.no_auth_seed_session
async def test_delegated_group_manager_cannot_create_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """The headline finding: ``system.groups.manage`` must NOT let a
    non-sysadmin mint a sysadmin-flagged group (which would confer
    sysadmin to its members)."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "backdoor-admins", "is_sysadmin": True}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"


@pytest.mark.no_auth_seed_session
async def test_delegated_group_manager_cannot_edit_group_to_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin with ``system.groups.manage`` cannot PATCH an
    existing group up to sysadmin."""
    group_id = _seed_group("g-target", "target")
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{group_id}",
        data=json.dumps({"is_sysadmin": True}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["error"] == "forbidden"


@pytest.mark.no_auth_seed_session
async def test_delegated_group_manager_cannot_clear_group_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """Clearing the bit is equally sysadmin-only: a non-sysadmin cannot
    demote an existing sysadmin group."""
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES ('g-admins', 'admins', 1, '2026-06-30T00:00:00')"
        )
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    resp = await client.patch(
        "/agent-mcp/api/router/groups/g-admins",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()


# ── Finding 1: delegation for non-sysadmin fields still works ──────


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_can_still_create_normal_user(
    aiohttp_client, router_app,
) -> None:
    """Regression guard: the sysadmin-grant lock does NOT break the
    legitimate delegated flow — a non-sysadmin with
    ``system.users.manage`` can still create ordinary users."""
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "normie",
            "password": "normiepassword",
            "is_sysadmin": False,
        }),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is False


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_can_edit_email(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin with the cap can edit non-privileged fields
    (email) as long as they don't touch ``is_sysadmin``."""
    victim_id = _seed_user("emailer", is_sysadmin=False)
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.patch(
        f"/agent-mcp/api/router/users/{victim_id}",
        data=json.dumps({"email": "emailer@example.test"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["user"]["email"] == "emailer@example.test"


# ── Finding 1: sysadmin CAN still grant sysadmin ───────────────────


async def test_sysadmin_can_create_sysadmin_user(
    aiohttp_client, router_app,
) -> None:
    """The auto-login sentinel operator is a sysadmin (first-user
    bootstrap); it may still mint sysadmin users."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "newadmin",
            "password": "newadminpassword",
            "is_sysadmin": True,
        }),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is True


async def test_sysadmin_can_create_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "real-admins", "is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["group"]["is_sysadmin"] is True


# ── Finding 2: group-cycle rejection via the real endpoint ─────────


async def _create_group(client, name: str) -> str:
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": name}),
        headers=_STRICT_ACCEPT,
    )
    return (await resp.json())["group"]["group_id"]


async def test_add_group_member_rejects_direct_cycle(
    aiohttp_client, router_app,
) -> None:
    """B∈A then A∈B must be rejected — the second edge would close a
    2-cycle in the membership DAG."""
    client = await aiohttp_client(router_app)
    a = await _create_group(client, "grp-a")
    b = await _create_group(client, "grp-b")

    # B ∈ A — fine.
    first = await client.post(
        f"/agent-mcp/api/router/groups/{a}/members",
        data=json.dumps({"group_id": b}),
        headers=_STRICT_ACCEPT,
    )
    assert first.status == 201, await first.text()

    # A ∈ B — would close a cycle; must be rejected.
    resp = await client.post(
        f"/agent-mcp/api/router/groups/{b}/members",
        data=json.dumps({"group_id": a}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False


async def test_add_group_member_rejects_self_loop(
    aiohttp_client, router_app,
) -> None:
    """A∈A is the trivial 1-cycle and must be rejected."""
    client = await aiohttp_client(router_app)
    a = await _create_group(client, "grp-self")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{a}/members",
        data=json.dumps({"group_id": a}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()


async def test_add_group_member_rejects_transitive_cycle(
    aiohttp_client, router_app,
) -> None:
    """C∈B, B∈A, then A∈C must be rejected (3-cycle)."""
    client = await aiohttp_client(router_app)
    a = await _create_group(client, "grp-ta")
    b = await _create_group(client, "grp-tb")
    c = await _create_group(client, "grp-tc")

    for parent, child in ((a, b), (b, c)):
        r = await client.post(
            f"/agent-mcp/api/router/groups/{parent}/members",
            data=json.dumps({"group_id": child}),
            headers=_STRICT_ACCEPT,
        )
        assert r.status == 201, await r.text()

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{c}/members",
        data=json.dumps({"group_id": a}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()


async def test_add_group_member_allows_valid_dag_edge(
    aiohttp_client, router_app,
) -> None:
    """A non-cyclic nested-group edge still succeeds (regression guard
    that the cycle check doesn't over-reject)."""
    client = await aiohttp_client(router_app)
    a = await _create_group(client, "grp-da")
    b = await _create_group(client, "grp-db")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{a}/members",
        data=json.dumps({"group_id": b}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["member"]["group_id"] == b
