"""PF-R18-1: ``int(float('inf'))`` overflow-coercion → HTTP 500.

``int()`` raises ``OverflowError`` (NOT ``ValueError``/``TypeError``)
for an infinite float. A JSON number token like ``1e400`` parses to
``float('inf')`` via ``json.loads``, so a caller-supplied
``{"limit": 1e400}`` slips past the ``except (TypeError, ValueError)``
numeric-coercion guards (which PF-R14-1 broadened for list/dict) and
falls through to the generic ``except Exception`` → live HTTP 500.

Two REST sites, both reproduced live:

1. ``POST /api/<project>/messages/query`` — ``limit`` / ``offset``
   coerced via ``int(data.get(...))``.
2. ``PATCH /agent-mcp/api/router/projects/<name>`` (rename) —
   ``grace_days`` coerced via ``int(grace_days_raw)``.

RED on origin/main (500 via uncaught ``OverflowError``); GREEN after
both guards broaden to ``except (TypeError, ValueError, OverflowError)``
so an overflowing numeric coerces to a clean 400 like the other
malformed-numeric cases. Regression coverage keeps the PF-R14-1
list/dict/string 400 behaviour and the valid-integer happy paths.

The payloads are sent as RAW JSON bodies (``content=`` / ``data=``)
so the wire carries the literal number token ``1e400`` — the exact
attacker shape — rather than the ``Infinity`` token ``json.dumps``
would emit for a Python ``float('inf')``.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


_JSON_CT = {"content-type": "application/json"}
_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── Site 1: messages/query limit + offset ───────────────────────────


async def test_query_overflow_limit_is_400_not_500(tmp_path) -> None:
    """An infinite ``limit`` (``1e400`` → ``inf``) must 400, not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/messages/query",
            content=(
                '{"token": "%s", "limit": 1e400}' % admin.admin_token
            ),
            headers=_JSON_CT,
        )
        assert resp.status_code == 400, (
            f"overflow limit must be a clean 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_overflow_offset_is_400_not_500(tmp_path) -> None:
    """An infinite ``offset`` (``1e400`` → ``inf``) must 400, not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/messages/query",
            content=(
                '{"token": "%s", "offset": 1e400}' % admin.admin_token
            ),
            headers=_JSON_CT,
        )
        assert resp.status_code == 400, (
            f"overflow offset must be a clean 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_valid_int_limit_offset_still_succeeds(tmp_path) -> None:
    """Regression: valid integer limit/offset still returns 2xx."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/messages/query",
            json={"token": admin.admin_token, "limit": 10, "offset": 0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["limit"] == 10
        assert body["offset"] == 0


async def test_query_string_limit_still_400(tmp_path) -> None:
    """Regression (PF-R14-1): a non-numeric string limit stays 400."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/messages/query",
            json={"token": admin.admin_token, "limit": "abc"},
        )
        assert resp.status_code == 400, (
            f"non-numeric string limit must be 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_list_limit_still_400(tmp_path) -> None:
    """Regression (PF-R14-1): a list-typed limit stays a clean 400."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/messages/query",
            json={"token": admin.admin_token, "limit": [1, 2]},
        )
        assert resp.status_code == 400, (
            f"list-typed limit must be 400, got "
            f"{resp.status_code}: {resp.text}"
        )


# ── Site 2: rename grace_days ───────────────────────────────────────


async def test_rename_overflow_grace_days_is_400_not_500(
    aiohttp_client, router_app, router_module, register_project,
    systemctl_stub,
) -> None:
    """An infinite ``grace_days`` (``1e400`` → ``inf``) must 400, not 500,
    with the project fully intact (no destructive step)."""
    ws = register_project("proj-a")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-a",
        data='{"name": "proj-b", "grace_days": 1e400}',
        headers={**_ACCEPT, **_JSON_CT},
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    # Registry untouched: old name present, new name absent.
    assert router_module._REGISTRY.get("proj-a") is not None
    assert router_module._REGISTRY.get("proj-b") is None

    # No destructive systemd stop ran.
    assert systemctl_stub.counts.get(
        ("stop", "agent-mcp@proj-a.service"), 0,
    ) == 0, "backend was stopped despite the 400"

    # Workspace dir not renamed.
    assert ws.exists(), "workspace dir vanished"
    assert not ws.with_name("proj-b").exists(), "workspace was renamed"


async def test_rename_valid_grace_days_still_ok(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Regression: a valid integer grace_days still renames (200)."""
    register_project("proj-c")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-c",
        json={"name": "proj-d", "grace_days": 30},
        headers=_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert router_module._REGISTRY.get("proj-d") is not None


async def test_rename_string_grace_days_still_400(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Regression (PF-R14-1 sibling): a non-numeric string grace_days
    stays a clean 400."""
    register_project("proj-e")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-e",
        json={"name": "proj-f", "grace_days": "abc"},
        headers=_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    assert router_module._REGISTRY.get("proj-e") is not None
    assert router_module._REGISTRY.get("proj-f") is None
