"""REST API for router-level groups — Phase 3 Wave 1b (prancy-napping-pie).

Operator-facing CRUD over the ``groups`` and ``group_membership``
tables introduced by Wave 1a. The group model supports nested
membership (a group can contain users AND other groups) with cycle
detection at insert time.

URL shape:
    GET    /agent-mcp/api/router/groups                       list groups
    POST   /agent-mcp/api/router/groups                       create group
    PATCH  /agent-mcp/api/router/groups/<group_id>            rename / set is_sysadmin
    DELETE /agent-mcp/api/router/groups/<group_id>            delete group
    GET    /agent-mcp/api/router/groups/<group_id>/members    list members
    POST   /agent-mcp/api/router/groups/<group_id>/members    add user or group
    DELETE /agent-mcp/api/router/groups/<group_id>/members/<member_id>
                                                              remove member

Member-add body: exactly one of ``{user_id: "..."}`` or
``{group_id: "..."}``. The DB CHECK constraint enforces XOR; the
handler surfaces violations as 400 errors before they hit the DB.

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
    """Helper: POST a user, return its user_id."""
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": username, "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    body = await resp.json()
    return body["user"]["user_id"]


async def _create_group(client, name: str, is_sysadmin: bool = False) -> str:
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": name, "is_sysadmin": is_sysadmin}),
        headers=_STRICT_ACCEPT,
    )
    body = await resp.json()
    return body["group"]["group_id"]


# ── GET /api/router/groups ──────────────────────────────────────────


async def test_list_groups_empty_by_default(
    aiohttp_client, router_app,
) -> None:
    """Fresh router.db has no groups; the empty list is wrapped in
    the success envelope (not 404)."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/groups", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["groups"] == []


# ── POST /api/router/groups ─────────────────────────────────────────


async def test_create_group_returns_row(
    aiohttp_client, router_app,
) -> None:
    """Creating a group returns 201 with the new row, including
    its server-assigned group_id."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "operators", "is_sysadmin": False}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["group"]["name"] == "operators"
    assert body["group"]["is_sysadmin"] is False
    assert "group_id" in body["group"]
    assert body["group"]["member_count"] == 0


async def test_create_group_rejects_duplicate_name(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    await _create_group(client, "ops")

    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "ops"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 409, f"got {resp.status}: {await resp.text()}"


async def test_create_group_rejects_missing_name(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


# ── PATCH /api/router/groups/<group_id> ─────────────────────────────


async def test_rename_group(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "old-name")

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{gid}",
        data=json.dumps({"name": "new-name"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["group"]["name"] == "new-name"


async def test_patch_group_is_sysadmin(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ops")

    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{gid}",
        data=json.dumps({"is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["group"]["is_sysadmin"] is True


async def test_patch_missing_group_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/groups/nope-id",
        data=json.dumps({"name": "x"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


# ── DELETE /api/router/groups/<group_id> ────────────────────────────


async def test_delete_group(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "doomed")

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{gid}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200

    listing = await client.get(
        "/agent-mcp/api/router/groups", headers=_STRICT_ACCEPT,
    )
    names = [g["name"] for g in (await listing.json())["groups"]]
    assert "doomed" not in names


# ── Members: add user ───────────────────────────────────────────────


async def test_add_user_member(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ops")
    uid = await _create_user(client, "ops-member")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["member"]["user_id"] == uid


async def test_add_nested_group_member(
    aiohttp_client, router_app,
) -> None:
    """Phase 3 supports nested groups — a group's member can itself
    be a group. Listing then reports both leaf users and child groups."""
    client = await aiohttp_client(router_app)
    parent = await _create_group(client, "parent-ops")
    child = await _create_group(client, "child-ops")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{parent}/members",
        data=json.dumps({"group_id": child}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["member"]["group_id"] == child


async def test_add_member_rejects_both_user_and_group(
    aiohttp_client, router_app,
) -> None:
    """The DB CHECK enforces XOR; the handler returns 400 before
    that fires so the caller gets a clean validation error."""
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ops")
    uid = await _create_user(client, "u1")
    other = await _create_group(client, "other-grp")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({"user_id": uid, "group_id": other}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


async def test_add_member_rejects_neither(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ops")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


# ── Members: list ───────────────────────────────────────────────────


async def test_list_members_returns_users_and_groups(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "parent")
    uid = await _create_user(client, "child-user")
    child_gid = await _create_group(client, "child-group")
    await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )
    await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({"group_id": child_gid}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.get(
        f"/agent-mcp/api/router/groups/{gid}/members",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    members = body["members"]
    assert any(
        m.get("user_id") == uid and m.get("username") == "child-user"
        for m in members
    )
    assert any(
        m.get("group_id") == child_gid and m.get("name") == "child-group"
        for m in members
    )


# ── Members: remove ─────────────────────────────────────────────────


async def test_remove_user_member(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "ops")
    uid = await _create_user(client, "leaving")
    await client.post(
        f"/agent-mcp/api/router/groups/{gid}/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{gid}/members/{uid}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    members = (await (await client.get(
        f"/agent-mcp/api/router/groups/{gid}/members",
        headers=_STRICT_ACCEPT,
    )).json())["members"]
    assert not any(m.get("user_id") == uid for m in members)


async def test_remove_group_member(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    parent = await _create_group(client, "parent")
    child = await _create_group(client, "child")
    await client.post(
        f"/agent-mcp/api/router/groups/{parent}/members",
        data=json.dumps({"group_id": child}),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.delete(
        f"/agent-mcp/api/router/groups/{parent}/members/{child}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200


# ── Auth gate ───────────────────────────────────────────────────────


@pytest.mark.no_auth_seed_session
async def test_groups_require_session(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/groups", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 401
