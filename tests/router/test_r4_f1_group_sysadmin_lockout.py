"""Security (round 4, finding R4-F1): group-conferred sysadmin has no
last-sysadmin lockout guard.

``_is_last_sysadmin`` (``admin_users_api.py``) is called only from
``edit_user_handler``/``delete_user_handler`` — the DIRECT-flag user
path. It was NEVER called from ``edit_group_handler`` (clearing a
group's ``is_sysadmin`` flag) or ``delete_group_handler`` (deleting an
``is_sysadmin = 1`` group). A group's ``is_sysadmin = 1`` flag confers
sysadmin transitively to every member (the same mechanism as the
AZ-1/AZ-2 amplification-guard family), so clearing/deleting the
system's LAST sysadmin-granting group silently zeroed the system's
sysadmin count with a plain 200, no 409 — locking the admin control
plane until the next router restart's
``bootstrap_first_operator_as_sysadmin`` self-heal.

Fix: ``_is_last_sysadmin_group`` unions direct-flag holders with
everyone who'd still be sysadmin via some OTHER ``is_sysadmin = 1``
group (via ``group_resolver.group_has_transitive_user_member``) to
decide whether THIS group's grant is the deployment's only remaining
path to sysadmin. Both ``edit_group_handler`` (clearing the bit) and
``delete_group_handler`` (deleting the group) now reject with the same
409 ``conflict`` shape ``_is_last_sysadmin``-triggered rejections
already use on the user path.

These tests go end-to-end through the same middleware + route stack
the production code uses (mirrors ``test_sec_r10_delete_group_guard``
and ``test_sec_r9_delete_sysadmin_guard``).
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror the test_sec_r10_delete_group_guard pattern) ──


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
    user_id = identity.create_user(username=username, password=password)
    with identity._connect() as conn:
        conn.execute(
            "UPDATE users SET is_sysadmin = ? WHERE user_id = ?",
            (1 if is_sysadmin else 0, user_id),
        )
    return user_id


def _demote_sentinel() -> None:
    """Clear the direct sysadmin bit on the auto-seeded sentinel
    operator (raw SQL — bypasses the API's own last-sysadmin guard so
    the test can set up a "sysadmin ONLY via group" fixture).

    MUST run AFTER the app server has started (i.e. after the first
    ``aiohttp_client(router_app)`` call): ``init_router_db``'s
    on_startup hook re-runs ``bootstrap_first_operator_as_sysadmin``,
    which self-heals by crowning the earliest-created user as sysadmin
    whenever NO user currently holds the direct flag. Demoting before
    the server starts would just get undone (or worse, crown a
    different user, e.g. a test-seeded ``alice``) by that self-heal.
    """
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "UPDATE users SET is_sysadmin = 0 WHERE username = 'test_sentinel_op'"
        )


def _seed_group(group_id: str, name: str, *, is_sysadmin: bool = False) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, ?, '2026-06-30T00:00:00')",
            (group_id, name, 1 if is_sysadmin else 0),
        )
    return group_id


def _group_is_sysadmin(group_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT is_sysadmin FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
    return bool(row["is_sysadmin"])


def _group_exists(group_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
    return row is not None


def _any_sysadmin_remaining() -> bool:
    """Ground truth: does ANY user resolve sysadmin=True right now
    (direct flag or transitive via a group)?"""
    from agent_mcp.router import group_resolver

    identity = _identity_module()
    with identity._connect() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        for row in rows:
            if group_resolver.resolve_user_is_sysadmin(
                row["user_id"], conn=conn,
            ):
                return True
    return False


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


async def _started_client_with_sentinel_demoted(aiohttp_client, router_app):
    """Start the app server (so ``init_router_db``'s on_startup bootstrap
    + self-heal fire exactly once and crown the sentinel), THEN demote
    the sentinel's direct flag. Returns the started client.

    Sequencing matters: demoting before the server starts would race
    the self-heal in ``bootstrap_first_operator_as_sysadmin`` (see
    ``_demote_sentinel``'s docstring).
    """
    client = await aiohttp_client(router_app)
    _demote_sentinel()
    return client


async def _add_as_group_sysadmin_and_login(client, group_id: str, username: str):
    """Seed ``username`` as a member of ``group_id`` (already
    ``is_sysadmin = 1``) and log them in. Returns the session cookie."""
    from agent_mcp.router import group_resolver

    user_id = _seed_user(username, is_sysadmin=False)
    group_resolver.add_group_member(group_id, member_user_id=user_id)
    return await _login(client, username)


# ── Finding R4-F1: clearing the last sysadmin-granting group ───────


@pytest.mark.no_auth_seed_session
async def test_clear_last_sysadmin_group_rejected(
    aiohttp_client, router_app,
) -> None:
    """PATCH ``is_sysadmin: false`` on the system's ONLY sysadmin-
    granting group must be rejected with 409, not a silent 200 that
    zeroes the sysadmin count."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    target = _seed_group("g-only-sysadmin", "Only Sysadmin Group", is_sysadmin=True)
    cookie = await _add_as_group_sysadmin_and_login(client, target, "alice")
    assert _any_sysadmin_remaining()  # sanity: alice is sysadmin via the group

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{target}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "conflict"
    # The group's grant must survive the rejected clear.
    assert _group_is_sysadmin(target)
    assert _any_sysadmin_remaining()


@pytest.mark.no_auth_seed_session
async def test_delete_last_sysadmin_group_rejected(
    aiohttp_client, router_app,
) -> None:
    """DELETE on the system's ONLY sysadmin-granting group must be
    rejected with 409, not a silent 200 that zeroes the sysadmin
    count."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    target = _seed_group("g-only-sysadmin-2", "Only Sysadmin Group 2", is_sysadmin=True)
    cookie = await _add_as_group_sysadmin_and_login(client, target, "alice")
    assert _any_sysadmin_remaining()

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "conflict"
    # The group must survive the rejected delete.
    assert _group_exists(target)
    assert _any_sysadmin_remaining()


# ── Happy path: NOT the last sysadmin path ──────────────────────────


async def test_clear_sysadmin_group_ok_when_direct_sysadmin_remains(
    aiohttp_client, router_app,
) -> None:
    """Clearing a sysadmin group's flag is fine when a DIRECT sysadmin
    (the sentinel) still exists — the target group is not the system's
    last path to sysadmin."""
    client = await aiohttp_client(router_app)
    target = _seed_group("g-clearable", "Clearable Group", is_sysadmin=True)

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{target}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["group"]["is_sysadmin"] is False
    assert not _group_is_sysadmin(target)


async def test_delete_sysadmin_group_ok_when_direct_sysadmin_remains(
    aiohttp_client, router_app,
) -> None:
    """Deleting a sysadmin group is fine when a DIRECT sysadmin (the
    sentinel) still exists."""
    client = await aiohttp_client(router_app)
    target = _seed_group("g-deletable", "Deletable Group", is_sysadmin=True)

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
    )

    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == target
    assert not _group_exists(target)


@pytest.mark.no_auth_seed_session
async def test_clear_sysadmin_group_ok_when_another_sysadmin_group_has_members(
    aiohttp_client, router_app,
) -> None:
    """Clearing a sysadmin group's flag is fine when ANOTHER
    sysadmin-flagged group still has a live member — that group is a
    real other path to sysadmin, so the target isn't the last one."""
    from agent_mcp.router import group_resolver

    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    other = _seed_group("g-other-sysadmin", "Other Sysadmin Group", is_sysadmin=True)
    bob_id = _seed_user("bob", is_sysadmin=False)
    group_resolver.add_group_member(other, member_user_id=bob_id)

    target = _seed_group("g-redundant", "Redundant Sysadmin Group", is_sysadmin=True)
    cookie = await _add_as_group_sysadmin_and_login(client, target, "alice")

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{target}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["group"]["is_sysadmin"] is False
    assert not _group_is_sysadmin(target)
    assert _any_sysadmin_remaining()  # bob, via `other`, is still sysadmin


@pytest.mark.no_auth_seed_session
async def test_delete_sysadmin_group_ok_when_another_sysadmin_group_has_members(
    aiohttp_client, router_app,
) -> None:
    """Deleting a sysadmin group is fine when ANOTHER sysadmin-flagged
    group still has a live member."""
    from agent_mcp.router import group_resolver

    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    other = _seed_group("g-other-sysadmin-2", "Other Sysadmin Group 2", is_sysadmin=True)
    bob_id = _seed_user("bob2", is_sysadmin=False)
    group_resolver.add_group_member(other, member_user_id=bob_id)

    target = _seed_group("g-redundant-2", "Redundant Sysadmin Group 2", is_sysadmin=True)
    cookie = await _add_as_group_sysadmin_and_login(client, target, "alice")

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert (await resp.json())["deleted"] == target
    assert not _group_exists(target)
    assert _any_sysadmin_remaining()


@pytest.mark.no_auth_seed_session
async def test_clear_sysadmin_group_rejected_when_other_sysadmin_group_is_empty(
    aiohttp_client, router_app,
) -> None:
    """An OTHER ``is_sysadmin = 1`` group with ZERO current members is
    not a live path to sysadmin — it must not save the target group
    from the last-sysadmin-group guard."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    _seed_group("g-empty-sysadmin", "Empty Sysadmin Group", is_sysadmin=True)

    target = _seed_group("g-only-live", "Only Live Sysadmin Group", is_sysadmin=True)
    cookie = await _add_as_group_sysadmin_and_login(client, target, "alice")

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{target}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    assert _group_is_sysadmin(target)
