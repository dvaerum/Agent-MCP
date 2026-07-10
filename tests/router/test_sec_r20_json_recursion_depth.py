"""PF-R20-1: deeply-nested JSON body → ``RecursionError`` → HTTP 500.

Python's ``json.loads`` raises ``RecursionError`` (a ``RuntimeError``,
NOT a ``ValueError``) when it recurses past the interpreter recursion
limit parsing a deeply-nested structure. The two aiohttp router-tier
body parsers only ``except json.JSONDecodeError`` — which is a
``ValueError`` subclass and so does NOT cover ``RecursionError`` — so a
~10k-deep JSON body slips the guard and propagates to an uncaught
HTTP 500 at the transport tier.

Two sites, both live-confirmed 500:

1. ``POST /api/router/projects`` — ``_parse_json_body`` in
   ``agent_mcp/router/app.py``.
2. ``POST /api/router/users`` — ``_json_body`` in
   ``agent_mcp/router/admin_users_api.py``.

RED on origin/main (500 via uncaught ``RecursionError``); GREEN after
both guards broaden to ``except (json.JSONDecodeError, RecursionError)``
so an over-deep body coerces to the SAME clean 400 the malformed-JSON
path already returns. Regression coverage keeps the valid-body happy
path, the ordinary-malformed 400, and the top-level-non-object 400.

The nested payload is sent as a RAW body (``data=``) so the wire
carries the literal ``[[[…]]]`` token — the exact attacker shape —
rather than anything ``json.dumps`` would re-encode.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# ~10k-deep nested JSON array: structurally valid JSON, but parsing it
# blows the recursion limit → ``RecursionError`` inside ``json.loads``.
_DEEP_DEPTH = 10000
_DEEP_JSON_BODY = "[" * _DEEP_DEPTH + "]" * _DEEP_DEPTH


# ── Site 1: POST /api/router/projects (app.py _parse_json_body) ──────


async def test_create_project_deep_json_is_400_not_500(
    aiohttp_client, router_app,
) -> None:
    """A ~10k-deep JSON body must 400 (clean malformed-body reject),
    not 500 via an uncaught ``RecursionError``."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=_DEEP_JSON_BODY,
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, (
        f"deep-nested body must be a clean 400, got "
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
        data=json.dumps({"name": "freshly-minted-r20"}),
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["project"]["name"] == "freshly-minted-r20"


async def test_create_project_ordinary_malformed_still_400(
    aiohttp_client, router_app,
) -> None:
    """Regression: an ordinary (non-deep) malformed JSON body stays 400."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data="{not valid json",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False


async def test_create_project_top_level_nonobject_still_400(
    aiohttp_client, router_app,
) -> None:
    """Regression: a valid JSON that is not an object (a shallow list)
    still 400s via the ``must be a JSON object`` guard — unchanged."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data="[1, 2, 3]",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False


# ── Site 2: POST /api/router/users (admin_users_api.py _json_body) ───


async def test_create_user_deep_json_is_400_not_500(
    aiohttp_client, router_app,
) -> None:
    """A ~10k-deep JSON body to the user-create endpoint must 400,
    not 500 via an uncaught ``RecursionError``."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=_DEEP_JSON_BODY,
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, (
        f"deep-nested body must be a clean 400, got "
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
            "username": "bob-r20",
            "password": "wonderlandsupersecret",
            "email": "bob-r20@example.test",
            "is_sysadmin": False,
        }),
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["user"]["username"] == "bob-r20"


async def test_create_user_ordinary_malformed_still_400(
    aiohttp_client, router_app,
) -> None:
    """Regression: an ordinary (non-deep) malformed JSON body stays 400."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data="{not valid json",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False


async def test_create_user_top_level_nonobject_still_400(
    aiohttp_client, router_app,
) -> None:
    """Regression: a valid JSON that is not an object still 400s via the
    ``must be a JSON object`` guard — unchanged."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data="[1, 2, 3]",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is False
