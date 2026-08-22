"""Security R15-F2 (HIGH, CONFIRMED, live-exploited) — admin_users_api's
``_json_body`` never routed through the shared JSON sanitizer.

FINDING: ``admin_users_api.py::_json_body`` parses every request body on
this REST surface with a bare ``json.loads(raw)`` — it never calls
``utils.json_utils.sanitize_json_input``/``_strip_control_bytes``/
``_strip_hidden_unicode``, the single shared chokepoint every other
JSON-body surface in the codebase uses (MCP ``tools/call``, the FastAPI
``app/routers/*`` tier via ``get_sanitized_json_body``). Class-sweep miss
on the R4-F3/R5-F8/R13-F2/R14-F3/R15-F1 sanitizer lineage.

Two confirmed live consequences, both reproduced here:

1. CRASH: an unpaired UTF-16 surrogate in ``email`` (via a JSON
   ``\\ud800``-style escape) survives unsanitized and crashes SQLite's
   UTF-8 TEXT bind with ``UnicodeEncodeError`` — bare 500 on both
   ``POST /users`` (create) and ``PATCH /users/<id>`` (edit, which has NO
   exception handling around its UPDATE at all).

2. SPOOFING: hidden-Unicode spoofing characters (zero-width space
   U+200B, RTLO U+202E) pass through completely unstripped into
   ``email`` and are echoed back verbatim by ``GET /users`` — the exact
   character classes R13-F2/R14-F3 strip on every other surface.

Also covers the defense-in-depth requirement: the sanitizer alone does
NOT strip an unpaired surrogate (it isn't in the ``Cf``/``Zl``/``Zp``
categories nor a variation selector), so routing ``_json_body`` through
``sanitize_json_input`` closes the spoofing vector but NOT the crash by
itself — an explicit UTF-8 encodability guard on ``email`` is required
too, and is asserted separately here.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# A JSON string carrying an unpaired UTF-16 surrogate escape. json.loads
# happily decodes \ud800 into a lone-surrogate Python str (valid Python,
# not valid Unicode text) -- this is what crashes the SQLite TEXT bind.
_SURROGATE_EMAIL_BODY = b'{"username": "surruser", "password": "longenoughpassword", "email": "abc\\ud800def@example.com"}'

_ZWSP = "\u200b"  # zero-width space
_RTLO = "\u202e"  # right-to-left override
_SPOOF_EMAIL = f"abc{_ZWSP}{_RTLO}def@example.com"


async def _create_user(client, username: str, password: str = "longenoughpassword") -> str:
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({"username": username, "password": password}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    return (await resp.json())["user"]["user_id"]


# ── Consequence 1: crash on create ─────────────────────────────────


async def test_create_user_with_unpaired_surrogate_email_no_bare_500(
    aiohttp_client, router_app,
) -> None:
    """An unpaired-surrogate email must be rejected with a clean 400 —
    never let ``UnicodeEncodeError`` escape as a bare 500."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=_SURROGATE_EMAIL_BODY,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False


# ── Consequence 1: crash on edit ───────────────────────────────────


async def test_edit_user_with_unpaired_surrogate_email_no_bare_500(
    aiohttp_client, router_app,
) -> None:
    """Same crash class on PATCH -- edit_user_handler has NO exception
    handling around its UPDATE at all pre-fix."""
    client = await aiohttp_client(router_app)
    user_id = await _create_user(client, "surredit")

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=b'{"email": "abc\\ud800def@example.com"}',
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False


# ── Consequence 2: spoofing survives unstripped ────────────────────


async def test_create_user_strips_hidden_unicode_from_email(
    aiohttp_client, router_app,
) -> None:
    """Zero-width space / RTLO in ``email`` must be stripped the same
    way R13-F2/R14-F3 strip them everywhere else -- not echoed back
    verbatim by a subsequent GET."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "spoofuser",
            "password": "longenoughpassword",
            "email": _SPOOF_EMAIL,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    created_email = body["user"]["email"]
    assert _ZWSP not in created_email, repr(created_email)
    assert _RTLO not in created_email, repr(created_email)

    listing = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )
    listed = next(
        u for u in (await listing.json())["users"]
        if u["username"] == "spoofuser"
    )
    assert _ZWSP not in listed["email"], repr(listed["email"])
    assert _RTLO not in listed["email"], repr(listed["email"])


# ── Regression: legitimate Unicode email still works ───────────────


async def test_create_user_with_real_non_latin_email_still_works(
    aiohttp_client, router_app,
) -> None:
    """A real internationalised email (non-Latin local-part/domain) must
    round-trip unchanged -- the fix must not over-strip legitimate
    content."""
    client = await aiohttp_client(router_app)
    real_email = "用户@例え.jp"

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "intluser",
            "password": "longenoughpassword",
            "email": real_email,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["user"]["email"] == real_email


# ── Sibling: identity.py's SSO/CLI provisioning path ───────────────
#
# Same bug CLASS as the REST-layer crash above, in the ONE OTHER place
# ``email`` reaches a ``users`` INSERT: ``identity.create_user`` (used by
# CLI bootstrap, the setup wizard, AND ``sso.find_or_create_sso_user``'s
# JIT-create fork for IdP-claim-derived email). Not live-testable via
# HTTP here (SSO isn't configured on this target) but fully testable at
# the function level -- exactly how the OIDC callback drives it.


async def test_identity_create_user_rejects_unpaired_surrogate_email(
    router_app,
) -> None:
    """``identity.create_user`` must raise a clean, typed error instead
    of letting a raw ``UnicodeEncodeError`` escape from the INSERT."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with pytest.raises(identity.InvalidEmailError):
        identity.create_user(
            username="idsurr",
            password="longenoughpassword",
            email="abc\ud800def@example.test",
        )
    assert identity.get_user_by_username("idsurr") is None


async def test_identity_create_user_strips_hidden_unicode_from_email(
    router_app,
) -> None:
    """Same hidden-Unicode strip as the REST layer, applied once for
    every ``create_user`` caller (CLI/SSO included)."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    identity.create_user(
        username="idspoof",
        password="longenoughpassword",
        email=_SPOOF_EMAIL,
    )
    row = identity.get_user_by_username("idspoof")
    assert row is not None
    assert _ZWSP not in row["email"], repr(row["email"])
    assert _RTLO not in row["email"], repr(row["email"])


async def test_sso_find_or_create_rejects_unpaired_surrogate_email(
    router_app,
) -> None:
    """The IdP-claim-derived JIT-create path must not crash on a
    malicious/misconfigured IdP's ``email`` claim -- mirrors the REST
    crash but through ``sso.find_or_create_sso_user`` exactly as
    ``handle_oidc_callback`` drives it."""
    import sys

    from agent_mcp.router import identity

    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")
    identity.run_router_migrations_upgrade()

    with pytest.raises(identity.InvalidEmailError):
        sso.find_or_create_sso_user(
            email="abc\ud800def@example.test",
            preferred_username="idpuser",
            subject=sso._oidc_subject("https://idp.example.test", "sub-1"),
            email_verified=False,
        )
    assert identity.get_user_by_username("idpuser") is None
