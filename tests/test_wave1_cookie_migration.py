"""Wave 1 cookie migration — RED tests for /api/tokens and /api/all-data.

Phase 1 PR D moved most dashboard mutation endpoints to
``require_operator_session`` cookie auth. Two GET endpoints lagged:

* ``GET /api/tokens`` — does a manual ``Authorization: Bearer`` check
  and otherwise admits unauthenticated callers (returns the
  ``admin_token`` to anyone with network reach). Tracked in plan file
  ``/home/dennis/.claude/plans/prancy-napping-pie.md`` Wave 1.

* ``GET /api/all-data`` — NO auth gate at all. Returns every agent /
  task / context / action row plus ``admin_token`` to anyone who can
  hit the URL.

This Wave 1 PR brings both behind ``Depends(require_operator_session)``
just like the mutation handlers. The dep already accepts the legacy
``Authorization: Bearer <admin_token>`` and body/query ``token=`` paths
for backwards compat (see ``agent_mcp/app/deps.py``), so admin-scripted
callers keep working. Unauthenticated callers now 401 instead of
getting a free admin token.

The RED protocol per ``feedback_tdd_red_green_for_bugs``:

* Each migrated endpoint gets THREE assertions:
  (a) valid cookie / valid bearer → 200
  (b) bearer-only with bogus token → 401
  (c) no auth at all → 401

The "valid cookie" path is exercised via the legacy bearer fallback in
``require_operator_session`` (the dep accepts cookie OR bearer OR body
OR query). Cookie wiring proper is integration-tested in
``tests/router/test_dashboard_session_auth.py``; this file pins the
FastAPI per-route gate.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ── GET /api/tokens ────────────────────────────────────────────────


async def test_tokens_endpoint_without_auth_returns_401(tmp_path) -> None:
    """No Authorization header, no cookie, no body-token → 401.

    Before Wave 1 this returned 200 + the admin_token to any caller,
    which is the issue O escalation surface the plan retires.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/tokens")
        assert r.status_code == 401, r.text


async def test_tokens_endpoint_with_admin_bearer_still_returns_200(tmp_path) -> None:
    """Legacy admin-bearer path stays valid (per ``app/deps.py`` Phase 1
    fallback). The dashboard never uses this path; admin scripts do.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["admin_token"] == admin.admin_token


async def test_tokens_endpoint_with_bogus_bearer_returns_401(tmp_path) -> None:
    """A non-admin / unknown bearer is rejected by the dep.

    Pre-Wave-1 this returned 403 via the manual ``verify_token(..., "admin")``
    check at routes.py:366. The dep now uniformly 401s every non-admin
    auth attempt (cookie / bearer / body-token are all admin-only).
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": "Bearer bogus-not-a-real-token"},
        )
        assert r.status_code == 401, r.text


# ── GET /api/all-data ──────────────────────────────────────────────


async def test_all_data_endpoint_without_auth_returns_401(tmp_path) -> None:
    """No Authorization header, no cookie → 401.

    Before Wave 1 this had NO auth gate at all and returned the full
    dashboard hydration blob (agents, tasks, context, admin_token) to
    anyone with network reach.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/all-data")
        assert r.status_code == 401, r.text


async def test_all_data_endpoint_with_admin_bearer_succeeds(tmp_path) -> None:
    """Legacy admin-bearer path stays valid for /api/all-data too —
    same fallback rationale as /api/tokens.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Sanity: response shape unchanged. Wave 2 strips admin_token;
        # Wave 1 leaves it so the frontend still builds.
        assert "agents" in body
        assert "tasks" in body
        assert "admin_token" in body


async def test_all_data_endpoint_with_bogus_bearer_returns_401(tmp_path) -> None:
    """Bogus bearer → 401 (matches the dep's uniform reject path)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/all-data",
            headers={"Authorization": "Bearer bogus-not-a-real-token"},
        )
        assert r.status_code == 401, r.text
