"""The REST surface no longer admits a token in the JSON body or the
query string.

Final piece of the token-delivery cleanup. Before this change,
``agent_mcp/app/deps.py::require_operator_session`` admitted an
operator-tier token delivered FIVE ways; two of those — the JSON
**body** ``{"token": ...}`` field and the **query-string**
``?token=<>`` param — were the last LEGACY doors. They are now GONE.

After this change the REST surface authenticates ONLY via:

  1. session cookie (dashboard),
  2. signed forwarding header (router),
  3. ``Authorization: Bearer`` header (operator-tier bearer).

These tests pin the two closed doors and the three kept doors on a
representative protected endpoint (``GET /api/tokens``). The
body/query assertions are the RED cases — they FAIL while the doors
are still open and pass once the admits are deleted.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_body_token_is_rejected(tmp_path) -> None:
    """A valid operator token in the JSON body → 401 (door closed).

    ``/api/tokens`` is a GET; a POST carrying the token in the body was
    previously admitted by the ``_legacy_body_token`` path regardless of
    method. We drive ``POST /api/agents/admin/edit`` (a protected
    mutation) with the token ONLY in the body and assert it is rejected.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/agents/admin/edit",
            json={"token": admin.admin_token, "capabilities": []},
        )
        assert r.status_code == 401, r.text
        assert admin.admin_token not in r.text


async def test_query_string_token_is_rejected(tmp_path) -> None:
    """A valid operator token in the query string → 401 (door closed)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(f"/api/tokens?token={admin.admin_token}")
        assert r.status_code == 401, r.text
        assert admin.admin_token not in r.text


async def test_forwarding_header_still_admits(tmp_path) -> None:
    """The KEPT signed-forwarding-header path (harness wrappers) works.

    Uses ``POST /api/agents/admin/edit`` rather than ``/api/tokens``:
    the tokens endpoint layers an extra operator-tier-*bearer*-only
    guard on top of ``require_operator_session`` and rejects the
    forwarding-header path, so it can't exercise this door.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/agents/admin/edit",
            json={"capabilities": []},
        )
        assert r.status_code == 200, r.text


async def test_authorization_bearer_still_admits(tmp_path) -> None:
    """The KEPT operator-tier ``Authorization: Bearer`` path works."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        assert "agent_tokens" in r.json()
