"""Round-13 security (PF-R13-1): strict-boolean parse for ``is_sysadmin``.

``is_sysadmin`` was coerced from the raw JSON body with bare ``bool()``
in ``agent_mcp.router.admin_users_api``. A caller sending a truthy
NON-boolean — the string ``"false"``, any non-empty string, a dict, a
list, a non-zero number — read as ``bool(...) == True`` and SILENTLY
MINTED a sysadmin user / group (or flipped an existing one). It is a
data-integrity footgun, not a privilege-escalation bypass (the
self-escalation gate uses the same coercion, so a non-sysadmin still
can't escalate), but a sysadmin operating the API could accidentally
grant sysadmin by passing a truthy-string.

The fix parses ``is_sysadmin`` STRICTLY: only a real JSON boolean
(``true``/``false``) is accepted; any other type is rejected with a 400
``validation_error`` envelope — the same tight, predictable contract as
the ``_reject_non_str`` (PF-R7-1) type-guards. ``isinstance(True, int)``
is ``True`` in Python but ``isinstance(1, bool)`` is ``False``, so the
guard also rejects a JSON number ``0``/``1``.

RED on origin/main: ``is_sysadmin: "false"`` mints a sysadmin (201).
GREEN after: the string is rejected 400 and no sysadmin is minted. The
regression tests confirm real JSON booleans (and the absent key) still
behave correctly, and the demote path still works.

The caller here is the auto-login sentinel operator, which the router
bootstrap promotes to sysadmin (it is the first user), so these
requests exercise the sysadmin-caller path where the footgun bites.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# Truthy non-boolean JSON values that ``bool()`` would read as True but
# a strict parse must reject. ``"false"`` is the headline case (a
# non-empty string is truthy). A dict/list/number round out the class.
_TRUTHY_NON_BOOL = ("false", "true", "0", "no", {"x": 1}, [1], 1, 2)
# Values that ``bool()`` reads as False but are still not real booleans.
_FALSY_NON_BOOL = ("", 0, [], {})


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _user_row(username: str):
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT user_id, is_sysadmin FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def _group_row(name: str):
    identity = _identity_module()
    with identity._connect() as conn:
        return conn.execute(
            "SELECT group_id, is_sysadmin FROM groups WHERE name = ?",
            (name,),
        ).fetchone()


async def _sentinel_user_id(client) -> str:
    resp = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )
    body = await resp.json()
    row = next(
        u for u in body["users"] if u["username"] == "test_sentinel_op"
    )
    return row["user_id"]


# ── create_user: strict reject of non-boolean is_sysadmin ──────────


@pytest.mark.parametrize("bad", _TRUTHY_NON_BOOL + _FALSY_NON_BOOL)
async def test_create_user_rejects_non_bool_is_sysadmin(
    aiohttp_client, router_app, bad,
) -> None:
    """A non-boolean ``is_sysadmin`` is rejected 400 and no user is
    created. RED on main for the truthy cases: ``"false"`` etc. minted a
    sysadmin (201)."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "r13user",
            "password": "longenoughpassword",
            "is_sysadmin": bad,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, (
        f"is_sysadmin={bad!r} should be 400, got {resp.status}: "
        f"{await resp.text()}"
    )
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"
    # No user minted at all — least surprise on a rejected create.
    assert _user_row("r13user") is None


# ── create_group: strict reject of non-boolean is_sysadmin ─────────


@pytest.mark.parametrize("bad", _TRUTHY_NON_BOOL + _FALSY_NON_BOOL)
async def test_create_group_rejects_non_bool_is_sysadmin(
    aiohttp_client, router_app, bad,
) -> None:
    """A non-boolean ``is_sysadmin`` on group-create is rejected 400 and
    no group is created."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "r13group", "is_sysadmin": bad}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, (
        f"is_sysadmin={bad!r} should be 400, got {resp.status}: "
        f"{await resp.text()}"
    )
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"
    assert _group_row("r13group") is None


# ── edit_user: strict reject of non-boolean is_sysadmin ────────────


async def test_edit_user_rejects_non_bool_is_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """PATCHing ``is_sysadmin: "false"`` on a plain user is rejected 400
    and the user's sysadmin bit is unchanged (stays 0). RED on main: the
    truthy string flipped the user TO sysadmin (200)."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "editme", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    user_id = (await create.json())["user"]["user_id"]
    assert _user_row("editme")["is_sysadmin"] == 0

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=json.dumps({"is_sysadmin": "false"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("error") == "validation_error"
    # Unchanged — the bad value did not flip the bit.
    assert _user_row("editme")["is_sysadmin"] == 0


# ── edit_group: strict reject of non-boolean is_sysadmin ───────────


async def test_edit_group_rejects_non_bool_is_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """PATCHing ``is_sysadmin: "false"`` on a plain group is rejected 400
    and the group's sysadmin bit is unchanged."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "editgroup"}),
        headers=_STRICT_ACCEPT,
    )
    group_id = (await create.json())["group"]["group_id"]
    assert _group_row("editgroup")["is_sysadmin"] == 0

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{group_id}",
        data=json.dumps({"is_sysadmin": "false"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    assert (await resp.json()).get("error") == "validation_error"
    assert _group_row("editgroup")["is_sysadmin"] == 0


# ============================ regressions ============================ #


async def test_create_user_true_bool_mints_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A real JSON ``true`` still mints a sysadmin (201)."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "realadmin",
            "password": "longenoughpassword",
            "is_sysadmin": True,
        }),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is True
    assert _user_row("realadmin")["is_sysadmin"] == 1


async def test_create_user_false_bool_is_not_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """A real JSON ``false`` yields a non-sysadmin user (201)."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "plainuser",
            "password": "longenoughpassword",
            "is_sysadmin": False,
        }),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is False
    assert _user_row("plainuser")["is_sysadmin"] == 0


async def test_create_user_absent_key_defaults_not_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """An absent ``is_sysadmin`` defaults to non-sysadmin (201)."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "defaultuser", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is False


async def test_create_group_true_bool_mints_sysadmin_group(
    aiohttp_client, router_app,
) -> None:
    """A real JSON ``true`` still mints a sysadmin group (201)."""
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "admingroup", "is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["group"]["is_sysadmin"] is True
    assert _group_row("admingroup")["is_sysadmin"] == 1


async def test_edit_user_true_bool_still_promotes(
    aiohttp_client, router_app,
) -> None:
    """Regression: PATCH ``is_sysadmin: true`` (real bool) promotes."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "promoteme", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    user_id = (await create.json())["user"]["user_id"]

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=json.dumps({"is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is True


async def test_edit_user_false_bool_demotes_when_another_sysadmin_exists(
    aiohttp_client, router_app,
) -> None:
    """Regression: the demote path (PATCH ``is_sysadmin: false`` real
    bool) still works when a second sysadmin remains — proving the
    strict parse feeds the ``demoting`` logic correctly."""
    client = await aiohttp_client(router_app)
    sentinel_id = await _sentinel_user_id(client)
    # Mint a second sysadmin so demoting the sentinel isn't a
    # last-sysadmin lockout.
    await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "backupadmin",
            "password": "longenoughpassword",
            "is_sysadmin": True,
        }),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{sentinel_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["user"]["is_sysadmin"] is False
    assert _user_row("test_sentinel_op")["is_sysadmin"] == 0


async def test_edit_group_false_bool_demotes(
    aiohttp_client, router_app,
) -> None:
    """Regression: PATCH ``is_sysadmin: false`` (real bool) clears a
    group's sysadmin bit."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "demotegroup", "is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )
    group_id = (await create.json())["group"]["group_id"]
    assert _group_row("demotegroup")["is_sysadmin"] == 1

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{group_id}",
        data=json.dumps({"is_sysadmin": False}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["group"]["is_sysadmin"] is False
    assert _group_row("demotegroup")["is_sysadmin"] == 0
