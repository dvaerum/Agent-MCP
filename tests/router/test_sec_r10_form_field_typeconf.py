"""Round-10 F2: form-POST field type-confusion (multipart file part).

The JSON-body type-confusion class was swept in rounds 7-9 via
``_reject_non_str``. This is the ``request.post()`` FORM analogue:
``login_post_handler`` and ``setup_post_handler`` read fields with
``form.get("username")`` and then call ``.strip()`` / hand the value
to argon2. aiohttp's ``request.post()`` returns a ``FileField`` (not a
``str``) for any field submitted as a multipart FILE part — a truthy
object with no ``.strip()`` and no meaning as a password. Pre-fix this
raised an ``AttributeError`` (username) or fed a ``FileField`` to the
hasher → an uncaught 500, reachable UNAUTHENTICATED.

Fix contract: a non-string field must fall through to the handler's
normal validation/auth failure (401 for login, 400 for setup), NEVER a
500. Normal multipart-text fields must keep working (regression).
"""

from __future__ import annotations

import io

import aiohttp
import pytest

pytestmark = pytest.mark.asyncio


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _file_part(value: bytes = b"payload") -> tuple[io.BytesIO, str, str]:
    """Args for FormData.add_field that force a multipart FILE part."""
    return (io.BytesIO(value), "evil.txt", "application/octet-stream")


# ── login: file part in a field must not 500 ───────────────────────


async def test_login_username_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """username submitted as a multipart file part → 401, never 500."""
    _identity_module().create_user(username="alice", password="hunter2")
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    fh, fn, ct = _file_part(b"alice")
    data.add_field("username", fh, filename=fn, content_type=ct)
    data.add_field("password", "hunter2")

    resp = await client.post(
        "/agent-mcp/login", data=data, allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


async def test_login_password_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """password as a multipart file part → 401 (bad creds), never 500.

    This one reaches argon2 verify pre-fix — a FileField handed to the
    hasher is the uncaught-500 path.
    """
    _identity_module().create_user(username="bob", password="hunter2")
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "bob")
    fh, fn, ct = _file_part(b"hunter2")
    data.add_field("password", fh, filename=fn, content_type=ct)

    resp = await client.post(
        "/agent-mcp/login", data=data, allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


async def test_login_multipart_text_field_still_works(
    aiohttp_client, router_app,
) -> None:
    """Regression: a genuine multipart-text login still authenticates."""
    _identity_module().create_user(username="carol", password="hunter2")
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "carol")
    data.add_field("password", "hunter2")

    resp = await client.post(
        "/agent-mcp/login", data=data, allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")


async def test_login_multipart_wrong_password_still_401(
    aiohttp_client, router_app,
) -> None:
    """Regression: wrong password over multipart still 401 (not 500)."""
    _identity_module().create_user(username="dave", password="hunter2")
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "dave")
    data.add_field("password", "wrongpw")

    resp = await client.post(
        "/agent-mcp/login", data=data, allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()


# ── setup: file part in a field must not 500 ───────────────────────


@pytest.mark.no_seed_operator
async def test_setup_username_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """setup username as a file part → 400 validation, never 500."""
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    fh, fn, ct = _file_part(b"first_op")
    data.add_field("username", fh, filename=fn, content_type=ct)
    data.add_field("password", "secret-pw-1234")
    data.add_field("password_confirm", "secret-pw-1234")

    resp = await client.post(
        "/agent-mcp/setup", data=data, allow_redirects=False,
    )
    assert resp.status == 400, await resp.text()
    assert _identity_module().get_user_by_username("first_op") is None


@pytest.mark.no_seed_operator
async def test_setup_password_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """setup password as a file part → 400 validation, never 500."""
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "first_op")
    fh, fn, ct = _file_part(b"secret-pw-1234")
    data.add_field("password", fh, filename=fn, content_type=ct)
    data.add_field("password_confirm", "secret-pw-1234")

    resp = await client.post(
        "/agent-mcp/setup", data=data, allow_redirects=False,
    )
    assert resp.status == 400, await resp.text()
    assert _identity_module().get_user_by_username("first_op") is None


@pytest.mark.no_seed_operator
async def test_setup_password_confirm_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """setup password_confirm as a file part → 400 mismatch, never 500."""
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "first_op")
    data.add_field("password", "secret-pw-1234")
    fh, fn, ct = _file_part(b"secret-pw-1234")
    data.add_field("password_confirm", fh, filename=fn, content_type=ct)

    resp = await client.post(
        "/agent-mcp/setup", data=data, allow_redirects=False,
    )
    assert resp.status == 400, await resp.text()
    assert _identity_module().get_user_by_username("first_op") is None


@pytest.mark.no_seed_operator
async def test_setup_email_as_file_part_is_not_500(
    aiohttp_client, router_app,
) -> None:
    """setup email as a file part must not 500.

    A non-string email is treated as absent (email is optional), so the
    handler proceeds with valid username+password → 303. The invariant
    under test is 'not a 500', not a specific status.
    """
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "first_op")
    data.add_field("password", "secret-pw-1234")
    data.add_field("password_confirm", "secret-pw-1234")
    fh, fn, ct = _file_part(b"ops@example.com")
    data.add_field("email", fh, filename=fn, content_type=ct)

    resp = await client.post(
        "/agent-mcp/setup", data=data, allow_redirects=False,
    )
    assert resp.status != 500, await resp.text()
    assert resp.status == 303, await resp.text()
    user = _identity_module().get_user_by_username("first_op")
    assert user is not None
    # Non-string email coerced to absent, not persisted as a FileField.
    assert user["email"] is None


@pytest.mark.no_seed_operator
async def test_setup_multipart_text_fields_still_work(
    aiohttp_client, router_app,
) -> None:
    """Regression: a genuine multipart-text setup still creates a user."""
    client = await aiohttp_client(router_app)

    data = aiohttp.FormData()
    data.add_field("username", "first_op")
    data.add_field("password", "secret-pw-1234")
    data.add_field("password_confirm", "secret-pw-1234")
    data.add_field("email", "ops@example.com")

    resp = await client.post(
        "/agent-mcp/setup", data=data, allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    user = _identity_module().get_user_by_username("first_op")
    assert user is not None
    assert user["email"] == "ops@example.com"
