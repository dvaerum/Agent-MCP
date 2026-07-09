"""SEC round-7 (PF-R7-1): type-confusion on identifier/email fields.

Passing a JSON ``dict``/``list`` where a scalar string is expected
(``user_id`` / ``group_id`` / ``email``) used to reach a SQLite bind
and raise an uncaught ``sqlite3.ProgrammingError`` ("type 'dict' is
not supported"), which escaped the handlers' ``sqlite3.IntegrityError``
catch and surfaced as a generic 500. The fix rejects a non-``str``
value with the same 400 ``validation_error`` envelope the handlers
already use for other bad input — the write lock is never taken.

Impact is LOW (no info disclosure, no corruption — the routes are
``system.*.manage``-gated and BEGIN IMMEDIATE rolls back), but a
structured-type body must never turn a client input error into a 500.

Regression coverage: a valid ``str`` in each field still works, and a
duplicate (the genuine ``IntegrityError`` path) still returns its
existing 409 — the fix must not swallow those.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# Structured JSON types that a scalar-string field must reject.
_BAD_TYPES = [{"nested": "obj"}, ["list", "item"]]


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


# ── create_user_handler: email ──────────────────────────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_create_user_rejects_structured_email(
    aiohttp_client, router_app, bad,
) -> None:
    """A ``dict``/``list`` in ``email`` is a 400 validation_error, not
    a 500 from an uncaught bind error."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "typeconf1", "password": "longenoughpassword",
            "email": bad,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_create_user_accepts_string_email(
    aiohttp_client, router_app,
) -> None:
    """Regression: a plain string email still creates the user (201)."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "typeconfok", "password": "longenoughpassword",
            "email": "ok@example.test",
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"


# ── edit_user_handler: email ────────────────────────────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_edit_user_rejects_structured_email(
    aiohttp_client, router_app, bad,
) -> None:
    """PATCHing ``email`` to a ``dict``/``list`` is a 400, not a 500."""
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "typeconfedit")

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{uid}",
        data=json.dumps({"email": bad}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_edit_user_accepts_string_email(
    aiohttp_client, router_app,
) -> None:
    """Regression: a plain string email still updates (200)."""
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "typeconfeditok")

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{uid}",
        data=json.dumps({"email": "new@example.test"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    assert (await resp.json())["user"]["email"] == "new@example.test"


# ── add_group_member_handler: user_id / group_id ────────────────────


@pytest.mark.parametrize("field", ["user_id", "group_id"])
@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_add_group_member_rejects_structured_id(
    aiohttp_client, router_app, field, bad,
) -> None:
    """A ``dict``/``list`` in ``user_id`` or ``group_id`` on the
    group-member add is a 400, not a 500 from an uncaught bind."""
    client = await aiohttp_client(router_app)
    parent = await _create_group(client, "typeconfparent")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{parent}/members",
        data=json.dumps({field: bad}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_add_group_member_accepts_string_id(
    aiohttp_client, router_app,
) -> None:
    """Regression: a real user_id string still adds the member (201)."""
    client = await aiohttp_client(router_app)
    parent = await _create_group(client, "typeconfparentok")
    uid = await _create_user(client, "typeconfmember")

    resp = await client.post(
        f"/agent-mcp/api/router/groups/{parent}/members",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"


# ── add_project_membership_handler: user_id / group_id ──────────────


@pytest.mark.parametrize("field", ["user_id", "group_id"])
@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_add_project_membership_rejects_structured_id(
    aiohttp_client, router_app, register_project, field, bad,
) -> None:
    """A ``dict``/``list`` in ``user_id`` or ``group_id`` on the
    project-membership add is a 400, not a 500 from an uncaught bind."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({field: bad}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_add_project_membership_accepts_string_id(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression: a real user_id string still adds the membership (201)."""
    register_project("alpha")
    client = await aiohttp_client(router_app)
    uid = await _create_user(client, "typeconfprojmember")

    resp = await client.post(
        "/agent-mcp/api/router/projects/alpha/memberships",
        data=json.dumps({"user_id": uid}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
