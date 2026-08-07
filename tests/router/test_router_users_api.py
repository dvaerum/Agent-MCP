"""REST API for router-level users — Phase 3 Wave 1b (prancy-napping-pie).

Adds operator-facing CRUD over the ``users`` table that lives in
``router.db``. The router admin REST surface (ADR 0014) hosts these
under ``/agent-mcp/api/router/users[/<user_id>]`` so they sit
alongside the existing ``/api/router/projects`` resource collection.

Wave 1b ENFORCEMENT NOTE: every handler is gated by
``require_operator_session_middleware`` — i.e. any authenticated
operator may invoke them. Wave 2 PR 3c overhauls enforcement so
``sysadmin`` is required for user CRUD; for now the UI is wired up
behind a permissive gate so we can ship the dashboard scaffolding
without coupling to the resolver work in Wave 2.

URL shape:
    GET    /agent-mcp/api/router/users                  list users
    POST   /agent-mcp/api/router/users                  create user
    PATCH  /agent-mcp/api/router/users/<user_id>        edit user (is_sysadmin, email)
    DELETE /agent-mcp/api/router/users/<user_id>        delete user

Each response uses the same JSON envelope shape as the existing
projects endpoints — ``{"success": true, ...}`` on 2xx, the
``_error_envelope`` discriminator on failure.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


# ── GET /api/router/users ───────────────────────────────────────────


async def test_list_users_returns_sentinel(
    aiohttp_client, router_app,
) -> None:
    """The sentinel operator seeded by the router-test fixture is
    visible in the user list. Each row carries the public-safe
    fields only: username, email, is_sysadmin, created_at,
    last_login_at — never the password_hash or user_id-as-secret."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert isinstance(body["users"], list)
    usernames = [u["username"] for u in body["users"]]
    assert "test_sentinel_op" in usernames
    row = next(u for u in body["users"] if u["username"] == "test_sentinel_op")
    # Public-safe shape only.
    assert "password_hash" not in row
    assert "user_id" in row
    assert "is_sysadmin" in row
    assert "email" in row  # may be None
    assert "last_login_at" in row


@pytest.mark.no_auth_seed_session
async def test_list_users_requires_session(
    aiohttp_client, router_app,
) -> None:
    """Without an operator session cookie the listing returns 401
    via the operator-session middleware."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 401


# ── POST /api/router/users ──────────────────────────────────────────


async def test_create_user_via_post(
    aiohttp_client, router_app,
) -> None:
    """Creating a user returns 201 with the public-safe row.
    Password is hashed server-side; never echoed back."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "alice",
            "password": "wonderlandsupersecret",
            "email": "alice@example.test",
            "is_sysadmin": False,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    user = body["user"]
    assert user["username"] == "alice"
    assert user["email"] == "alice@example.test"
    assert user["is_sysadmin"] is False
    assert "password" not in user
    assert "password_hash" not in user


async def test_create_user_rejects_duplicate_username(
    aiohttp_client, router_app,
) -> None:
    """Duplicate usernames map to a 409 — the unique constraint is
    surfaced as a clean error envelope, not a 500."""
    client = await aiohttp_client(router_app)
    await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "bob", "password": "passwordpassword",
        }),
        headers=_STRICT_ACCEPT,
    )

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "bob", "password": "differentpassword",
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 409, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error")  # discriminated error


async def test_create_user_rejects_short_password(
    aiohttp_client, router_app,
) -> None:
    """A trivially-short password is rejected with the 400
    validation-error envelope before any DB work. The error shape
    (``success: false`` + ``error: "validation_error"``) is the
    contract the dashboard renders."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({"username": "weak", "password": "x"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"


async def test_create_user_rejects_password_below_min_12(
    aiohttp_client, router_app,
) -> None:
    """Round-3 hardening: admin user-create now enforces the CANONICAL
    min-12 password policy (``identity.validate_password_strength``),
    not the old inline min-8 validator. An 11-char password — accepted
    under the old min-8 policy — is now rejected with the same 400
    weak-password envelope, closing the policy-drift gap where an
    admin-created user could carry a weaker password than a
    self-provisioned one."""
    client = await aiohttp_client(router_app)

    eleven_chars = "abcdefghij1"  # 11 chars: passed min-8, fails min-12
    assert len(eleven_chars) == 11
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "shortpw", "password": eleven_chars,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body.get("success") is False
    assert body.get("error") == "validation_error"
    assert "12" in body.get("message", "")


async def test_create_user_accepts_min_12_password(
    aiohttp_client, router_app,
) -> None:
    """A password at exactly the min-12 boundary is accepted (201)."""
    client = await aiohttp_client(router_app)

    twelve_chars = "abcdefghij12"  # exactly 12 chars
    assert len(twelve_chars) == 12
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "boundarypw", "password": twelve_chars,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["user"]["username"] == "boundarypw"


async def test_create_user_rejects_missing_fields(
    aiohttp_client, router_app,
) -> None:
    """username + password are required by the body model."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({"username": "only-name"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400


# ── PATCH /api/router/users/<user_id> ───────────────────────────────


async def test_edit_user_sets_is_sysadmin(
    aiohttp_client, router_app,
) -> None:
    """The edit handler accepts a partial body — anything not
    supplied stays untouched. is_sysadmin is the headline Wave 1b
    field because Phase 3 introduces the sysadmin tier."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "carol", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    user_id = (await create.json())["user"]["user_id"]

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=json.dumps({"is_sysadmin": True, "email": "carol@x.test"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["user"]["is_sysadmin"] is True
    assert body["user"]["email"] == "carol@x.test"


async def test_edit_user_missing_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/users/deadbeefdeadbeef",
        data=json.dumps({"is_sysadmin": True}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404


# ── DELETE /api/router/users/<user_id> ──────────────────────────────


async def test_delete_user_drops_row(
    aiohttp_client, router_app,
) -> None:
    """Deleting a user removes them from the listing. Foreign-key
    cascades on sessions + project_membership are handled at the
    DB layer (ON DELETE CASCADE in the initial migration)."""
    client = await aiohttp_client(router_app)
    create = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "dave", "password": "longenoughpassword",
        }),
        headers=_STRICT_ACCEPT,
    )
    user_id = (await create.json())["user"]["user_id"]

    resp = await client.delete(
        f"/agent-mcp/api/router/users/{user_id}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True

    listing = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )
    usernames = [u["username"] for u in (await listing.json())["users"]]
    assert "dave" not in usernames


async def test_delete_user_missing_returns_404(
    aiohttp_client, router_app,
) -> None:
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/users/deadbeefdeadbeef",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 404
