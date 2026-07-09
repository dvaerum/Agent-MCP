"""Security (round 9): deleting a sysadmin account is sysadmin-only.

Finding AZ-R9-1 [MED] — guard asymmetry between demote and delete.
    ``edit_user_handler`` blocks a non-sysadmin operator (one whose group
    carries ``system.users.manage``) from CLEARING a sysadmin's
    ``is_sysadmin`` bit (403 ``_forbid_sysadmin_write``). But
    ``delete_user_handler`` had only the ``_is_last_sysadmin`` guard, so
    the same delegate could DELETE a sysadmin account outright (200) as
    long as ≥2 sysadmins existed — delete strictly superseded demote,
    defeating the demote guard's intent. The fix mirrors the demote
    guard: deleting a sysadmin account requires the CALLER to be a
    sysadmin, else 403.

These tests go end-to-end through the same middleware + route stack the
production code uses (mirrors ``test_sec_sysadmin_escalation``).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}
_STRICT_ACCEPT = _REST_HEADERS


# ── Helpers (mirror the test_sec_sysadmin_escalation pattern) ──────


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


async def _login(
    client, username: str, password: str = "passwordpassword"
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


def _user_exists(user_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
    return row is not None


# ── Finding AZ-R9-1: delegate cannot DELETE a sysadmin ─────────────


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_cannot_delete_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin operator with ``system.users.manage`` cannot DELETE
    a sysadmin account — mirrors the demote (clear-bit) 403 guard. Two
    sysadmins are present so the ``_is_last_sysadmin`` guard is NOT what
    fires; this asserts the NEW sysadmin-target guard specifically."""
    # Two sysadmins present (sentinel bootstrap + this one) so deleting
    # 'victim' would leave ≥1 sysadmin — the last-sysadmin guard cannot
    # be what rejects the request.
    victim_id = _seed_user("victim-admin", is_sysadmin=True)
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    # The victim must survive the rejected delete.
    assert _user_exists(victim_id)


@pytest.mark.no_auth_seed_session
async def test_delegated_user_manager_can_still_delete_normal_user(
    aiohttp_client, router_app,
) -> None:
    """Regression: the guard fires ONLY for sysadmin targets. A
    non-sysadmin delegate may still delete an ordinary (non-sysadmin)
    user."""
    victim_id = _seed_user("normie", is_sysadmin=False)
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )
    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == victim_id
    assert not _user_exists(victim_id)


async def test_sysadmin_can_delete_non_last_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin caller (auto-login sentinel) may delete a sysadmin
    account as long as it isn't the last one."""
    # Create the client first: the sentinel operator is bootstrapped on
    # server start, so seeding users beforehand would leave the users
    # table non-empty and suppress the sentinel bootstrap (breaking the
    # auto-login). With the sentinel present, seeding 'deletable-admin'
    # yields TWO sysadmins, so its delete isn't the last-sysadmin case.
    client = await aiohttp_client(router_app)
    victim_id = _seed_user("deletable-admin", is_sysadmin=True)
    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == victim_id
    assert not _user_exists(victim_id)


async def test_delete_last_sysadmin_still_conflicts(
    aiohttp_client, router_app,
) -> None:
    """The pre-existing last-sysadmin guard is preserved: even a sysadmin
    cannot delete the final sysadmin account (409)."""
    identity = _identity_module()
    client = await aiohttp_client(router_app)
    # The auto-login sentinel is the first (and only) sysadmin.
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE is_sysadmin = 1 LIMIT 1",
        ).fetchone()
    assert row is not None
    last_id = row["user_id"]
    resp = await client.delete(
        f"/agent-mcp/api/router/users/{last_id}",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    assert (await resp.json())["success"] is False
    assert _user_exists(last_id)
