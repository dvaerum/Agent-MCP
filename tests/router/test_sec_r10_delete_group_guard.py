"""Security (round 10): deleting a sysadmin-flagged group is sysadmin-only.

Finding AZ-R10-1 [MED] — guard asymmetry between group demote and group
    delete (the group sibling of the round-9 ``delete_user_handler`` fix).
    ``edit_group_handler`` blocks a non-sysadmin operator (one whose group
    carries only ``system.groups.manage``) from CLEARING a group's
    ``is_sysadmin`` bit (403 ``_forbid_sysadmin_write``). But
    ``delete_group_handler`` had NO caller-sysadmin guard, so the same
    delegate could DELETE a sysadmin-flagged group outright (200) —
    destroying the group-conferred sysadmin grant to every member, a
    privileged action they are barred from via demote. The fix mirrors
    the demote guard: deleting a sysadmin-flagged group requires the
    CALLER to be a sysadmin, else 403.

These tests go end-to-end through the same middleware + route stack the
production code uses (mirrors ``test_sec_r9_delete_sysadmin_guard``).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}
_STRICT_ACCEPT = _REST_HEADERS


# ── Helpers (mirror the test_sec_r9_delete_sysadmin_guard pattern) ──


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


def _group_exists(group_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
    return row is not None


# ── Finding AZ-R10-1: delegate cannot DELETE a sysadmin group ──────


@pytest.mark.no_auth_seed_session
async def test_delegated_group_manager_cannot_delete_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """A non-sysadmin operator with only ``system.groups.manage`` cannot
    DELETE a sysadmin-flagged group — mirrors the demote (clear-bit) 403
    guard in ``edit_group_handler``."""
    target = _seed_group(
        "g-sysadmin", "Sysadmin Group", is_sysadmin=True,
    )
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    # The sysadmin-flagged group must survive the rejected delete.
    assert _group_exists(target)


@pytest.mark.no_auth_seed_session
async def test_delegated_group_manager_can_still_delete_normal_group(
    aiohttp_client, router_app,
) -> None:
    """Regression: the guard fires ONLY for sysadmin-flagged targets. A
    non-sysadmin delegate may still delete an ordinary (non-sysadmin)
    group."""
    target = _seed_group(
        "g-normal", "Ordinary Group", is_sysadmin=False,
    )
    client, cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )
    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == target
    assert not _group_exists(target)


async def test_sysadmin_can_delete_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """A sysadmin caller (auto-login sentinel) may delete a
    sysadmin-flagged group."""
    client = await aiohttp_client(router_app)
    target = _seed_group(
        "g-sysadmin-ok", "Sysadmin Group", is_sysadmin=True,
    )
    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == target
    assert not _group_exists(target)
