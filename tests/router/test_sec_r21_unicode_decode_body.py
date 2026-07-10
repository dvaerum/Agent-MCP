"""PF-R21-1: invalid-UTF8 request body/form → uncaught ``UnicodeDecodeError``
→ HTTP 500.

Python's ``json.loads(bytes)`` and aiohttp's ``await request.post()`` on a
urlencoded body both attempt a UTF-8 decode of the raw wire bytes. Invalid
UTF-8 (e.g. ``b"\\xff\\xfe"``) makes that decode raise ``UnicodeDecodeError``
— which is a ``ValueError`` subclass but NOT a ``json.JSONDecodeError``. The
round-20 fix broadened the two JSON parsers to
``except (json.JSONDecodeError, RecursionError)`` — which still does NOT
catch ``UnicodeDecodeError`` — so an invalid-UTF8 body slips the guard and
propagates to an uncaught HTTP 500. The two form-tier sites never guarded
``request.post()`` at all.

Four live-confirmed 500 sites:

1. ``POST /api/router/projects`` — ``_parse_json_body`` (app.py, JSON tier).
2. ``POST /api/router/users``    — ``_json_body`` (admin_users_api.py, JSON).
3. ``POST /agent-mcp/login``     — ``request.post()`` (login.py, form tier;
   UNAUTHENTICATED — an unauth attacker can 500 the login endpoint).
4. ``POST /agent-mcp/setup``     — ``request.post()`` (setup_wizard.py, form
   tier; bootstrap window).

RED on origin/main (500 via uncaught ``UnicodeDecodeError``); GREEN after the
JSON guards broaden to ``except (ValueError, RecursionError)`` (ValueError
covers both JSONDecodeError and UnicodeDecodeError) and the two form sites
wrap ``request.post()`` in ``except (ValueError, UnicodeDecodeError)`` →
each endpoint's existing malformed-input path (400 for JSON, 401 for login,
400 for the wizard), NOT a 500. Regression coverage keeps the valid happy
paths and the ordinary-malformed 400.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


# A body whose UTF-8 decode raises ``UnicodeDecodeError``: the raw ``\xff``
# byte is never a valid UTF-8 lead byte. The leading ``{"k":"`` is
# deliberate — ``json.loads`` sniffs a leading ``\xff\xfe`` as a UTF-16 BOM
# and would decode it cleanly, so the invalid byte is embedded mid-body
# (after an ASCII prefix that pins the encoding sniff to UTF-8). This shape
# raises ``UnicodeDecodeError`` on BOTH ``json.loads(bytes)`` (JSON tier)
# and aiohttp's ``request.post()`` UTF-8 form decode (form tier).
_INVALID_UTF8 = b'{"k":"\xff\xfe\xfd"}'

_JSON_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}
_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


# ── Site 1: POST /api/router/projects (app.py _parse_json_body) ──────


async def test_create_project_invalid_utf8_is_400_not_500(
    aiohttp_client, router_app,
) -> None:
    """An invalid-UTF8 JSON body must 400 (clean malformed-body reject),
    not 500 via an uncaught ``UnicodeDecodeError``."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=_INVALID_UTF8,
        headers=_JSON_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 400, (
        f"invalid-UTF8 body must be a clean 400, got "
        f"{resp.status}: {await resp.text()}"
    )
    body = await resp.json()
    assert body["success"] is False


async def test_create_project_valid_body_still_succeeds(
    aiohttp_client, router_app,
) -> None:
    """Regression: an ordinary valid create body still returns 201."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "freshly-minted-r21"}),
        headers=_JSON_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True


# ── Site 2: POST /api/router/users (admin_users_api.py _json_body) ───


async def test_create_user_invalid_utf8_is_400_not_500(
    aiohttp_client, router_app,
) -> None:
    """An invalid-UTF8 JSON body to the user-create endpoint must 400,
    not 500 via an uncaught ``UnicodeDecodeError``."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=_INVALID_UTF8,
        headers=_JSON_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 400, (
        f"invalid-UTF8 body must be a clean 400, got "
        f"{resp.status}: {await resp.text()}"
    )
    body = await resp.json()
    assert body["success"] is False


async def test_create_user_valid_body_still_succeeds(
    aiohttp_client, router_app,
) -> None:
    """Regression: an ordinary valid create body still returns 201."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "bob-r21",
            "password": "wonderlandsupersecret",
            "email": "bob-r21@example.test",
            "is_sysadmin": False,
        }),
        headers=_JSON_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["user"]["username"] == "bob-r21"


# ── Site 3: POST /agent-mcp/login (login.py request.post()) ──────────


async def test_login_invalid_utf8_form_is_401_not_500(
    aiohttp_client, router_app,
) -> None:
    """An invalid-UTF8 urlencoded login body must return the same clean
    401 an invalid login returns — NOT a 500. This endpoint is
    unauthenticated, so a 500 here is an unauth attacker DoS/oracle."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/login",
        data=_INVALID_UTF8,
        headers=_FORM_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 401, (
        f"invalid-UTF8 login body must be a clean 401, got "
        f"{resp.status}: {await resp.text()}"
    )
    # No session cookie leaks on the malformed path.
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "agent_mcp_session" not in set_cookie


async def test_login_valid_form_still_authenticates(
    aiohttp_client, router_app,
) -> None:
    """Regression: a valid urlencoded login still 303s + sets a cookie.

    Seeds a user via the identity module, then hits the same handler
    with a well-formed body."""
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    identity.create_user(username="carol-r21", password="rightpw-r21")

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "carol-r21", "password": "rightpw-r21"},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    assert "agent_mcp_session" in resp.headers.get("Set-Cookie", "")


# ── Site 4: POST /agent-mcp/setup (setup_wizard.py request.post()) ───


@pytest.mark.no_seed_operator
async def test_setup_invalid_utf8_form_is_4xx_not_500(
    aiohttp_client, router_app,
) -> None:
    """An invalid-UTF8 urlencoded body to the bootstrap wizard must
    return the wizard's clean invalid-input status (400), NOT a 500.

    ``no_seed_operator`` keeps the users table empty so the handler
    reaches ``request.post()`` rather than 303-bouncing to /login."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/setup",
        data=_INVALID_UTF8,
        headers=_FORM_HEADERS,
        allow_redirects=False,
    )

    assert resp.status == 400, (
        f"invalid-UTF8 wizard body must be a clean 400, got "
        f"{resp.status}: {await resp.text()}"
    )


@pytest.mark.no_seed_operator
async def test_setup_valid_form_still_creates_operator(
    aiohttp_client, router_app,
) -> None:
    """Regression: a valid urlencoded wizard submission still creates the
    first operator and 303-redirects."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "firstop-r21",
            "password": "wonderlandsupersecret",
            "password_confirm": "wonderlandsupersecret",
            "email": "firstop-r21@example.test",
        },
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
