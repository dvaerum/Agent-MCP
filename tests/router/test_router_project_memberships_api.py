"""REST API for project memberships — Phase 3 Wave 1b (prancy-napping-pie).

Operator-facing CRUD over ``project_membership``. Wave 1a extended
the table with a ``role`` column (operator/viewer) and an optional
``group_id`` column so a row can grant access to either a single
user or a whole group.

URL shape:
    GET    /agent-mcp/api/router/projects/<name>/memberships
                                                              list rows
    POST   /agent-mcp/api/router/projects/<name>/memberships
                                                              add user or group
    PATCH  /agent-mcp/api/router/projects/<name>/memberships/<membership_id>
                                                              change role
    DELETE /agent-mcp/api/router/projects/<name>/memberships/<membership_id>
                                                              remove row

Membership-id shape: ``u:<user_id>`` or ``g:<group_id>`` — a
synthetic surrogate because ``project_membership`` doesn't carry a
single autoincrement PK. The prefix disambiguates so callers can
mutate by id without a separate "is this a user or group row?"
query parameter.

Add body: exactly one of ``{user_id: "..."}`` / ``{group_id: "..."}``
plus optional ``{role: "operator"|"viewer"}`` (default "operator").

Wave 1b ENFORCEMENT NOTE: see test_router_users_api.py docstring.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def _create_user(client, username: str) -> str:
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": username, "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    return (await resp.json())["user"]["user_id"]


async def _create_group(client, name: str) -> str:
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": name}),
        headers=_STRICT_ACCEPT,
    )
    return (await resp.json())["group"]["group_id"]


# ── GET memberships ─────────────────────────────────────────────────


async def test_list_memberships_includes_creator(
    aiohttp_client, router_app, register_project,
) -> None:
    """The ``register_project`` fixture grants the sentinel operator
    membership in the project (the Phase 1 retroactive-grant). The
    listing surfaces that row."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/memberships",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    rows = body["memberships"]
    assert any(
        r.get("username") == "test_sentinel_op" for r in rows
    ), f"sentinel operator missing from listing: {rows!r}"


async def test_list_memberships_unknown_project_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/no-such-project/memberships",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


# ── POST: add user membership ───────────────────────────────────────


async def test_add_user_membership_defaults_to_operator(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "newop")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["membership"]["user_id"] == uid
    assert body["membership"]["role"] == "operator"


async def test_add_user_membership_with_viewer_role(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "readonly")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid, "role": "viewer"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201
    body = await resp.json()
    assert body["membership"]["role"] == "viewer"


async def test_add_group_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "engineers")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"group_id": gid, "role": "operator"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["membership"]["group_id"] == gid
    assert body["membership"]["role"] == "operator"


async def test_add_membership_rejects_both_user_and_group(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "x")
    gid = await _create_group(client, "y")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid, "group_id": gid}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


async def test_add_membership_rejects_unknown_role(
    aiohttp_client, router_app, register_project,
) -> None:
    """Role is constrained to operator|viewer at the body model;
    'admin' or anything else is a 400. The DB has the same CHECK
    constraint as a defence-in-depth layer."""
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "admin-wanna-be")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid, "role": "admin"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


# ── PATCH: change role ──────────────────────────────────────────────


async def test_change_role(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "promoted")
    await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid, "role": "viewer"}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.patch(
        f"/agent-mcp/api/router/projects/alpha/memberships/u:{uid}",
        data=json.dumps({"role": "operator"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["membership"]["role"] == "operator"


async def test_change_role_unknown_membership_returns_404(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/alpha/memberships/u:nope",
        data=json.dumps({"role": "operator"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


# ── DELETE membership ───────────────────────────────────────────────


async def test_delete_user_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "kicked")
    await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/alpha/memberships/u:{uid}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200

    rows = (await (await client.get(
        "/agent-mcp/api/router/projects/alpha/memberships",
        headers=_STRICT_ACCEPT,
    )).json())["memberships"]
    assert not any(r.get("user_id") == uid for r in rows)


async def test_delete_group_membership(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ex-team")
    await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"group_id": gid}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/projects/alpha/memberships/g:{gid}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200


# ── Auth gate ───────────────────────────────────────────────────────


@pytest.mark.no_auth_seed_session
async def test_memberships_require_session(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/alpha/memberships",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 401
