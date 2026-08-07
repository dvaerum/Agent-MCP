"""SEC round-8 (PF-R8-1): type-confusion 500 on router name/username fields.

Round 7 (PF-R7-1) guarded ``user_id`` / ``group_id`` / ``email`` with
``_reject_non_str``, but the five router name/username fields read via
``(body.get(X) or "").strip()`` BEFORE their ``isinstance`` validator
still raised an uncaught ``AttributeError`` → 500 on a non-string JSON
value (``X or ""`` returns ``X``, then ``X.strip()`` blows up):

  * ``POST   /api/router/projects``        → ``name``
  * ``PATCH  /api/router/projects/<p>``    → ``name`` (rename)
  * ``POST   /api/router/users``           → ``username``
  * ``POST   /api/router/groups``          → ``name``
  * ``PATCH  /api/router/groups/<id>``     → ``name``

The fix validates the field is a ``str`` BEFORE the ``.strip()`` and
returns the handler's existing 400 envelope. RED against origin/main:
each of these returns 500 on a ``dict`` / ``list`` value.

Regression: a valid string still succeeds on every handler.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# Structured JSON types (plus a bare int) a scalar-string field must reject.
_BAD_TYPES = [{"nested": "obj"}, ["list", "item"], 123]


# ── POST /api/router/projects : name ────────────────────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_create_project_rejects_non_string_name(
    aiohttp_client, router_app, bad,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": bad}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "invalid_name"


async def test_create_project_accepts_string_name(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "typeok-proj"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"


# ── PATCH /api/router/projects/<p> : name (rename) ──────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_rename_project_rejects_non_string_name(
    aiohttp_client, router_app, register_project, bad,
) -> None:
    register_project("renamesrc")
    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/renamesrc",
        data=json.dumps({"name": bad}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "invalid_name"


async def test_rename_project_accepts_string_name(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("renameok")
    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/renameok",
        data=json.dumps({"name": "renamed-ok"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"


# ── POST /api/router/users : username ───────────────────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_create_user_rejects_non_string_username(
    aiohttp_client, router_app, bad,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({"username": bad, "password": "longenoughpassword"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_create_user_accepts_string_username(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "typeokuser", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"


# ── POST /api/router/groups : name ──────────────────────────────────


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_create_group_rejects_non_string_name(
    aiohttp_client, router_app, bad,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": bad}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_create_group_accepts_string_name(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": "typeokgroup"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"


# ── PATCH /api/router/groups/<id> : name ────────────────────────────


async def _create_group(client, name: str) -> str:
    resp = await client.post(
        "/agent-mcp/api/router/groups",
        data=json.dumps({"name": name}),
        headers=_STRICT_ACCEPT,
    )
    return (await resp.json())["group"]["group_id"]


@pytest.mark.parametrize("bad", _BAD_TYPES)
async def test_edit_group_rejects_non_string_name(
    aiohttp_client, router_app, bad,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "editgrp")
    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{gid}",
        data=json.dumps({"name": bad}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_edit_group_accepts_string_name(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)
    gid = await _create_group(client, "editgrpok")
    resp = await client.patch(
        f"/agent-mcp/api/router/groups/{gid}",
        data=json.dumps({"name": "editgrp-renamed"}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
