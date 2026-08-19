"""Security (round 5, finding R5-F4): sysadmin-lockout guards resolve
LOCAL sysadmin state instead of TRANSITIVE sysadmin status.

R4-F1 (PR #662) guarded exactly two vectors that can drop a
deployment's effective sysadmin population to zero: clearing a
group's OWN ``is_sysadmin`` flag, and deleting an ``is_sysadmin = 1``
group row (``_is_last_sysadmin_group``, folded into
``admin_users_api._no_sysadmin_would_remain`` by this fix). Four more
vectors shared the identical root cause — a check that reads a LOCAL
column instead of resolving the TRANSITIVE membership graph
(``group_resolver``):

  1. ``remove_group_member_handler`` — draining a sysadmin group's
     SOLE live member had NO last-sysadmin check at all.
  2. ``delete_group_handler``'s own sysadmin-only gate (AZ-R10-1) keyed
     on the TARGET group's own ``is_sysadmin`` column, not whether it
     is transitively sysadmin (itself OR any ancestor).
  3. ``delete_user_handler``'s cascade only checked the deleted row's
     own direct flag, never whether the user was the sole live path to
     some OTHER ``is_sysadmin = 1`` group (their ``group_membership``
     row cascades away via ``ON DELETE CASCADE``).
  4. ``delete_group_handler``'s nested-group cascade only checked the
     deleted group's own flag, never ANCESTOR groups referencing it
     via ``member_group_id``.

Fix: every one of the four mutation paths now re-evaluates the GLOBAL
"does any user still have effective sysadmin access" invariant AFTER
performing its mutation (inside the existing ``BEGIN IMMEDIATE``),
rolling back with the same 409 ``conflict`` shape when the answer
would be "no" — ``admin_users_api._no_sysadmin_would_remain``.
``delete_group_handler``'s own authz gate additionally now resolves
``group_resolver.group_is_transitively_sysadmin`` instead of the
group's own column, mirroring what ``add_group_member_handler`` /
``remove_group_member_handler``'s amplification guards already do.

These tests go end-to-end through the same middleware + route stack
the production code uses (mirrors ``test_r4_f1_group_sysadmin_lockout``
and ``test_sec_r12_revoke_amplification``).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── Helpers (mirror test_r4_f1_group_sysadmin_lockout / test_sec_r10) ──


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
    """Clear the direct sysadmin bit on the auto-seeded sentinel operator
    (raw SQL — bypasses the API's own last-sysadmin guard). MUST run
    AFTER the app server has started (see
    ``test_r4_f1_group_sysadmin_lockout._demote_sentinel``'s docstring
    for the self-heal race this sequencing avoids)."""
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


def _grant_capability(group_id: str, *caps: str) -> None:
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _group_exists(group_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,),
        ).fetchone()
    return row is not None


def _user_exists(user_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
    return row is not None


def _membership_exists(group_id: str, member_id: str) -> bool:
    identity = _identity_module()
    with identity._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM group_membership WHERE group_id = ? AND "
            "(member_user_id = ? OR member_group_id = ?)",
            (group_id, member_id, member_id),
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
    client = await aiohttp_client(router_app)
    _demote_sentinel()
    return client


async def _add_as_group_sysadmin_and_login(client, group_id: str, username: str):
    """Seed ``username`` as a member of ``group_id`` (already
    ``is_sysadmin = 1``) and log them in. Returns (user_id, cookie)."""
    from agent_mcp.router import group_resolver

    user_id = _seed_user(username, is_sysadmin=False)
    group_resolver.add_group_member(group_id, member_user_id=user_id)
    cookie = await _login(client, username)
    return user_id, cookie


async def _delegated_client(aiohttp_client, router_app, *caps: str):
    """Log in a non-sysadmin operator 'alice-delegate' who carries
    ``caps`` via a group capability grant. Returns (client, cookie).

    Spins up its OWN client via ``aiohttp_client(router_app)`` — safe
    only when no earlier client in the test has demoted the sentinel:
    aiohttp's ``TestServer.start_server()`` re-fires the app's
    ``on_startup`` hooks per server instance, which re-runs
    ``bootstrap_first_operator_as_sysadmin``'s self-heal and would
    re-crown a demoted sentinel (see ``_demote_sentinel``'s docstring).
    Tests that demote the sentinel must use ``_add_delegate_and_login``
    on their EXISTING client instead.
    """
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice-delegate", is_sysadmin=False)
    group_id = _seed_group("g-delegated-r5f4", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice-delegate")
    return client, cookie


async def _add_delegate_and_login(
    client, username: str, group_id: str, *caps: str,
) -> str:
    """Seed a non-sysadmin delegate carrying ``caps`` via a group
    capability grant and log them into the EXISTING ``client`` (no new
    server/client — see ``_delegated_client``'s docstring for why a
    second one would undo a prior ``_demote_sentinel``). Returns the
    session cookie."""
    from agent_mcp.router import group_resolver

    delegate_id = _seed_user(username, is_sysadmin=False)
    _seed_group(group_id, f"{group_id}-name")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=delegate_id)
    return await _login(client, username)


# ══ Vector 1 — remove_group_member_handler drains a group's sole ═══
#    live member with NO last-sysadmin check at all.


@pytest.mark.no_auth_seed_session
async def test_remove_sole_member_of_last_sysadmin_group_rejected(
    aiohttp_client, router_app,
) -> None:
    """RED: removing the SOLE live member of the deployment's only
    ``is_sysadmin = 1`` group must be rejected with 409 — draining the
    group has the identical end-state as clearing its flag or deleting
    it outright, but had no guard at all.

    On origin/main this returns 200 and zeroes the deployment's
    sysadmin count."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    target = _seed_group("g-r5f4-solo", "Solo Sysadmin Group", is_sysadmin=True)
    alice_id, cookie = await _add_as_group_sysadmin_and_login(
        client, target, "alice-solo",
    )
    assert _any_sysadmin_remaining()  # sanity: alice is sysadmin via the group

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}/members/{alice_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "conflict"
    # The membership must survive the rejected removal.
    assert _membership_exists(target, alice_id)
    assert _any_sysadmin_remaining()


async def test_remove_group_member_ok_when_direct_sysadmin_remains(
    aiohttp_client, router_app,
) -> None:
    """GREEN: removing a sysadmin group's sole member is fine when a
    DIRECT sysadmin (the sentinel) still exists — the group is not the
    deployment's last path to sysadmin."""
    client = await aiohttp_client(router_app)
    target = _seed_group("g-r5f4-solo-ok", "Solo Group", is_sysadmin=True)
    alice_id, cookie = await _add_as_group_sysadmin_and_login(
        client, target, "alice-solo-ok",
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}/members/{alice_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _membership_exists(target, alice_id)
    assert _any_sysadmin_remaining()  # sentinel is still sysadmin


@pytest.mark.no_auth_seed_session
async def test_remove_group_member_ok_when_group_has_another_live_member(
    aiohttp_client, router_app,
) -> None:
    """GREEN: removing one member from a sysadmin group is fine when
    ANOTHER member of the SAME group remains — that member is still a
    real live path to sysadmin."""
    from agent_mcp.router import group_resolver

    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    target = _seed_group("g-r5f4-pair", "Pair Group", is_sysadmin=True)
    alice_id, cookie = await _add_as_group_sysadmin_and_login(
        client, target, "alice-pair",
    )
    bob_id = _seed_user("bob-pair", is_sysadmin=False)
    group_resolver.add_group_member(target, member_user_id=bob_id)

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{target}/members/{alice_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _membership_exists(target, alice_id)
    assert _any_sysadmin_remaining()  # bob is still sysadmin via the group


# ══ Vector 3 — delete_user_handler's cascade only checks the ═══════
#    deleted row's own direct flag, never a group-conferred path.


@pytest.mark.no_auth_seed_session
async def test_delete_sole_group_sysadmin_user_rejected(
    aiohttp_client, router_app,
) -> None:
    """RED: deleting a user who holds NO direct flag but is the SOLE
    live member of the deployment's only ``is_sysadmin = 1`` group must
    be rejected with 409 — the cascading ``group_membership`` delete
    zeroes the deployment's sysadmin count exactly like deleting the
    group itself would.

    Caller is a non-sysadmin delegate holding only
    ``system.users.manage`` — the victim's own direct flag is 0 so the
    existing AZ-R9-1 authz gate does not fire; only the (new) lockout
    check can catch this. On origin/main this returns 200."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    target = _seed_group("g-r5f4-userdel", "User Delete Group", is_sysadmin=True)
    victim_id, _victim_cookie = await _add_as_group_sysadmin_and_login(
        client, target, "victim-userdel",
    )
    assert _any_sysadmin_remaining()

    delegate_cookie = await _add_delegate_and_login(
        client, "alice-userdel", "g-r5f4-userdel-delegate", "system.users.manage",
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": delegate_cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "conflict"
    assert _user_exists(victim_id)
    assert _any_sysadmin_remaining()


@pytest.mark.no_auth_seed_session
async def test_delete_group_sysadmin_user_ok_when_another_sysadmin_group_has_members(
    aiohttp_client, router_app,
) -> None:
    """GREEN: deleting a group-only-sysadmin user is fine when ANOTHER
    ``is_sysadmin = 1`` group still has a live member — that group is a
    real other path to sysadmin."""
    from agent_mcp.router import group_resolver

    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    other = _seed_group("g-r5f4-userdel-other", "Other Group", is_sysadmin=True)
    bob_id = _seed_user("bob-userdel", is_sysadmin=False)
    group_resolver.add_group_member(other, member_user_id=bob_id)

    target = _seed_group("g-r5f4-userdel-2", "User Delete Group 2", is_sysadmin=True)
    victim_id, _victim_cookie = await _add_as_group_sysadmin_and_login(
        client, target, "victim-userdel-2",
    )

    delegate_cookie = await _add_delegate_and_login(
        client, "alice-userdel-2", "g-r5f4-userdel-2-delegate", "system.users.manage",
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": delegate_cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _user_exists(victim_id)
    assert _any_sysadmin_remaining()  # bob, via `other`, is still sysadmin


async def test_delete_group_sysadmin_user_ok_when_direct_sysadmin_remains(
    aiohttp_client, router_app,
) -> None:
    """GREEN: deleting a group-only-sysadmin user is fine when a DIRECT
    sysadmin (the sentinel) still exists."""
    client = await aiohttp_client(router_app)
    target = _seed_group("g-r5f4-userdel-3", "User Delete Group 3", is_sysadmin=True)
    victim_id, _victim_cookie = await _add_as_group_sysadmin_and_login(
        client, target, "victim-userdel-3",
    )

    delegate_client, delegate_cookie = await _delegated_client(
        aiohttp_client, router_app, "system.users.manage",
    )

    resp = await delegate_client.delete(
        f"/agent-mcp/api/router/users/{victim_id}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": delegate_cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _user_exists(victim_id)


# ══ Vectors 2 & 4 — delete_group_handler's own authz gate + nested ═
#    cascade both key on the TARGET group's own column, not the
#    transitive graph.


async def _seed_nested_sysadmin_chain(
    client, *, ancestor_id: str, mid_id: str, victim_username: str,
):
    """Group ``ancestor_id`` (is_sysadmin=1) ⊃ group ``mid_id``
    (is_sysadmin=0, member of ancestor) ⊃ a fresh victim user (sole
    member of ``mid_id``). The victim inherits sysadmin transitively
    through ``mid_id`` → ``ancestor_id``. Returns (victim_id, cookie)."""
    from agent_mcp.router import group_resolver

    _seed_group(ancestor_id, f"{ancestor_id}-name", is_sysadmin=True)
    _seed_group(mid_id, f"{mid_id}-name", is_sysadmin=False)
    group_resolver.add_group_member(ancestor_id, member_group_id=mid_id)
    victim_id = _seed_user(victim_username, is_sysadmin=False)
    group_resolver.add_group_member(mid_id, member_user_id=victim_id)
    cookie = await _login(client, victim_username)
    return victim_id, cookie


@pytest.mark.no_auth_seed_session
async def test_delegate_cannot_delete_unflagged_ancestor_sysadmin_path(
    aiohttp_client, router_app,
) -> None:
    """RED (vector 2, authz): a non-sysadmin delegate holding only
    ``system.groups.manage`` must NOT be able to DELETE an UNFLAGGED
    intermediate group that is the sole path from an ``is_sysadmin = 1``
    ancestor down to a live member — deleting it severs the victim's
    inherited sysadmin exactly like deleting a directly-flagged group
    would, but the old gate only inspected the target's own
    ``is_sysadmin`` column (0 here) and let it straight through.

    The sentinel stays a direct sysadmin throughout, so this isolates
    the AUTHZ gate: even though the deployment would NOT be zeroed out,
    the delegate must still be blocked from stripping the victim's
    sysadmin. On origin/main this returns 200."""
    client = await aiohttp_client(router_app)
    victim_id, _victim_cookie = await _seed_nested_sysadmin_chain(
        client,
        ancestor_id="g-r5f4-anc-1",
        mid_id="g-r5f4-mid-1",
        victim_username="victim-nested-1",
    )
    assert _any_sysadmin_remaining()

    delegate_client, delegate_cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )

    resp = await delegate_client.delete(
        "/agent-mcp/api/router/groups/g-r5f4-mid-1",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": delegate_cookie},
        allow_redirects=False,
    )

    assert resp.status == 403, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "forbidden"
    assert _group_exists("g-r5f4-mid-1")
    from agent_mcp.router import group_resolver

    identity = _identity_module()
    with identity._connect() as conn:
        assert group_resolver.resolve_user_is_sysadmin(victim_id, conn=conn)


@pytest.mark.no_auth_seed_session
async def test_self_delete_of_sole_ancestor_path_group_rejected(
    aiohttp_client, router_app,
) -> None:
    """RED (vector 4, lockout): the victim of the nested chain is
    themselves sysadmin (transitively, via the unflagged intermediate
    group) and so passes the AUTHZ gate — but deleting that intermediate
    group cascades away their OWN group_membership row, zeroing the
    deployment's sysadmin count (no direct sysadmin, no other
    ``is_sysadmin`` group). The old lockout check only ever inspected
    the deleted group's own ``is_sysadmin`` column (0 here) and never
    consulted the ancestor, so this sailed through with zero lockout
    check consulted. Expect 409.

    On origin/main this returns 200."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    _victim_id, victim_cookie = await _seed_nested_sysadmin_chain(
        client,
        ancestor_id="g-r5f4-anc-2",
        mid_id="g-r5f4-mid-2",
        victim_username="victim-nested-2",
    )
    assert _any_sysadmin_remaining()

    resp = await client.delete(
        "/agent-mcp/api/router/groups/g-r5f4-mid-2",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": victim_cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "conflict"
    assert _group_exists("g-r5f4-mid-2")
    assert _any_sysadmin_remaining()


@pytest.mark.no_auth_seed_session
async def test_delete_unflagged_ancestor_path_group_ok_when_direct_sysadmin_remains(
    aiohttp_client, router_app,
) -> None:
    """GREEN: a sysadmin caller (the victim, transitively sysadmin via
    the chain) may delete the unflagged intermediate group when a
    DIRECT sysadmin (a separately-seeded root) still exists — the
    victim's own group is not the deployment's last path to sysadmin."""
    client = await _started_client_with_sentinel_demoted(aiohttp_client, router_app)
    _seed_user("root-nested-3", is_sysadmin=True)
    _victim_id, victim_cookie = await _seed_nested_sysadmin_chain(
        client,
        ancestor_id="g-r5f4-anc-3",
        mid_id="g-r5f4-mid-3",
        victim_username="victim-nested-3",
    )

    resp = await client.delete(
        "/agent-mcp/api/router/groups/g-r5f4-mid-3",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": victim_cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _group_exists("g-r5f4-mid-3")
    assert _any_sysadmin_remaining()  # root-nested-3 is still sysadmin


async def test_delegate_can_still_delete_group_unrelated_to_any_sysadmin_path(
    aiohttp_client, router_app,
) -> None:
    """GREEN regression: the transitive authz gate fires ONLY for a
    group that is transitively sysadmin. A non-sysadmin delegate may
    still delete an ordinary group with no sysadmin ancestor."""
    target = _seed_group("g-r5f4-ordinary", "Ordinary Group", is_sysadmin=False)

    delegate_client, delegate_cookie = await _delegated_client(
        aiohttp_client, router_app, "system.groups.manage",
    )

    resp = await delegate_client.delete(
        f"/agent-mcp/api/router/groups/{target}",
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": delegate_cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert not _group_exists(target)
