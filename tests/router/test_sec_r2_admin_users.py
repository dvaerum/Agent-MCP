"""Security round-2 pentest-loop fixes — router admin users/groups API.

Owner-authorized defensive security. Three findings, each exercised
end-to-end through the real middleware + route stack:

Finding 1 [MED] — escalate by JOINING a pre-existing sysadmin group.
    ``add_group_member_handler`` is gated only on ``system.groups.manage``
    with no sysadmin-caller check. #288 closed SETTING ``is_sysadmin`` and
    CREATING a sysadmin group, but not JOINING one: a delegated
    (non-sysadmin) ``system.groups.manage`` operator could add themselves —
    or nest a group they control — into a (transitively) sysadmin-flagged
    group and inherit sysadmin via the resolver's transitive closure. The
    fix requires the CALLER to already be sysadmin when the PARENT group is
    transitively sysadmin-flagged.

Finding 2 [LOW-MED] — ``group_membership`` add is not idempotent. Only a
    CHECK, no UNIQUE, and no handler pre-check, so a double-submit produced
    two 201s and a double-counted membership. The fix adds partial UNIQUE
    indices + surfaces the IntegrityError as a 409.

Finding 3 [LOW] — last-sysadmin lockout. ``edit_user_handler`` (clearing
    ``is_sysadmin``) and ``delete_user_handler`` had no guard, so the last
    sysadmin could be demoted/deleted, leaving nobody able to grant
    sysadmin. The fix rejects any write that would drop the sysadmin count
    to zero.
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


# ── Helpers (mirror test_sec_sysadmin_escalation.py) ───────────────


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


def _seed_group(group_id: str, name: str, *, is_sysadmin: bool = False) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, ?, '2026-06-30T00:00:00')",
            (group_id, name, 1 if is_sysadmin else 0),
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
    group capability grant. Returns (client, cookie, alice_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id


def _member_count(group_id: str) -> int:
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM group_membership WHERE group_id = ?",
            (group_id,),
        ).fetchone()["n"]


def _sysadmin_count() -> int:
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_sysadmin = 1"
        ).fetchone()["n"]


async def _sentinel_user_id(client) -> str:
    resp = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )
    body = await resp.json()
    row = next(u for u in body["users"] if u["username"] == "test_sentinel_op")
    return row["user_id"]


# ── Finding 1: JOIN-a-sysadmin-group escalation ────────────────────


@pytest.mark.no_auth_seed_session
async def test_delegated_manager_cannot_add_self_to_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin ``system.groups.manage`` operator cannot add
    themselves as a member of a pre-existing sysadmin-flagged group —
    that would confer sysadmin via the resolver's transitive closure."""
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-sysadmin", "Real Admins", is_sysadmin=True)

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-sysadmin/members",
        data=json.dumps({"user_id": alice_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert _member_count("g-sysadmin") == 0


@pytest.mark.no_auth_seed_session
async def test_delegated_manager_cannot_nest_group_into_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """Nesting a controlled group into a sysadmin group is the same
    escalation via a different member kind — also denied 403."""
    client, cookie, _alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-sysadmin", "Real Admins", is_sysadmin=True)
    _seed_group("g-pawn", "Pawn Group")

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-sysadmin/members",
        data=json.dumps({"group_id": "g-pawn"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    assert _member_count("g-sysadmin") == 0


@pytest.mark.no_auth_seed_session
async def test_delegated_manager_cannot_join_transitively_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """The parent group itself is NOT flagged, but it is a member of a
    sysadmin group, so a member of the parent still inherits sysadmin.
    Joining the parent must be denied 403."""
    from agent_mcp.router import group_resolver

    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-top", "Top Admins", is_sysadmin=True)
    _seed_group("g-mid", "Middle Group")
    # g-mid ∈ g-top  → members of g-mid inherit sysadmin from g-top.
    group_resolver.add_group_member("g-top", member_group_id="g-mid")

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-mid/members",
        data=json.dumps({"user_id": alice_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    assert _member_count("g-mid") == 0


@pytest.mark.no_auth_seed_session
async def test_delegated_manager_can_add_to_normal_group(
    aiohttp_client, router_app,
) -> None:
    """Regression: adding a member to a NON-sysadmin group still works
    for a delegated operator (the guard doesn't over-reject)."""
    client, cookie, alice_id = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    _seed_group("g-plain", "Plain Group")

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-plain/members",
        data=json.dumps({"user_id": alice_id}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 201, await resp.text()
    assert _member_count("g-plain") == 1


async def test_sysadmin_can_add_to_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """Regression: a real sysadmin (the auto-login sentinel) may still
    add members to a sysadmin-flagged group."""
    client = await aiohttp_client(router_app)
    _seed_group("g-admins2", "Admins Two", is_sysadmin=True)
    target = _seed_user("targetuser", is_sysadmin=False)

    resp = await client.post(
        "/agent-mcp/api/router/groups/g-admins2/members",
        data=json.dumps({"user_id": target}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert _member_count("g-admins2") == 1


# ── Finding 2: idempotent add_group_member ─────────────────────────


async def test_double_add_user_member_is_conflict(
    aiohttp_client, router_app,
) -> None:
    """Two identical user-member adds: first 201, second 409, count 1."""
    client = await aiohttp_client(router_app)
    _seed_group("g-dup", "Dup Group")
    uid = _seed_user("dupuser", is_sysadmin=False)

    first = await client.post(
        "/agent-mcp/api/router/groups/g-dup/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )
    assert first.status == 201, await first.text()

    second = await client.post(
        "/agent-mcp/api/router/groups/g-dup/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )
    assert second.status == 409, await second.text()
    assert (await second.json())["success"] is False
    assert _member_count("g-dup") == 1


async def test_double_add_group_member_is_conflict(
    aiohttp_client, router_app,
) -> None:
    """Two identical group-member adds: first 201, second 409, count 1."""
    client = await aiohttp_client(router_app)
    _seed_group("g-parent", "Parent")
    _seed_group("g-child", "Child")

    first = await client.post(
        "/agent-mcp/api/router/groups/g-parent/members",
        data=json.dumps({"group_id": "g-child"}),
        headers=_STRICT_ACCEPT,
    )
    assert first.status == 201, await first.text()

    second = await client.post(
        "/agent-mcp/api/router/groups/g-parent/members",
        data=json.dumps({"group_id": "g-child"}),
        headers=_STRICT_ACCEPT,
    )
    assert second.status == 409, await second.text()
    assert _member_count("g-parent") == 1


# ── Finding 3: last-sysadmin lockout ───────────────────────────────


async def test_cannot_demote_last_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """Clearing ``is_sysadmin`` on the only sysadmin is rejected 409 and
    a sysadmin remains."""
    client = await aiohttp_client(router_app)
    sentinel_id = await _sentinel_user_id(client)
    assert _sysadmin_count() == 1

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{sentinel_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    assert (await resp.json())["success"] is False
    assert _sysadmin_count() == 1


async def test_cannot_delete_last_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """Deleting the only sysadmin is rejected 409 and a sysadmin
    remains."""
    client = await aiohttp_client(router_app)
    sentinel_id = await _sentinel_user_id(client)
    assert _sysadmin_count() == 1

    resp = await client.delete(
        f"/agent-mcp/api/router/users/{sentinel_id}",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    assert _sysadmin_count() == 1


async def test_can_demote_sysadmin_when_another_exists(
    aiohttp_client, router_app,
) -> None:
    """Regression: demotion is allowed when a second sysadmin remains."""
    client = await aiohttp_client(router_app)
    sentinel_id = await _sentinel_user_id(client)
    # Mint a second sysadmin via the sysadmin sentinel caller.
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "secondadmin",
            "password": "secondadminpassword",
            "is_sysadmin": True,
        }),
        headers=_STRICT_ACCEPT,
    )
    assert create.status == 201, await create.text()
    assert _sysadmin_count() == 2

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{sentinel_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is False
    assert _sysadmin_count() == 1


async def test_can_delete_sysadmin_when_another_exists(
    aiohttp_client, router_app,
) -> None:
    """Regression: deletion is allowed when a second sysadmin remains."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "thirdadmin",
            "password": "thirdadminpassword",
            "is_sysadmin": True,
        }),
        headers=_STRICT_ACCEPT,
    )
    victim_id = (await create.json())["user"]["user_id"]
    assert _sysadmin_count() == 2

    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert _sysadmin_count() == 1
