"""GET /api/tokens must not return admin_token to non-admin bearers.

UPSTREAM_ISSUES.md issue O (filed during this plan execution). Today
`/api/tokens` returns the admin token in plaintext to any HTTP
caller. Anyone on the network who can reach the endpoint can
escalate to admin by curling `/api/tokens`. Same shape as issue I
(view_project_context) but via the REST surface.

Fix: when the request carries `Authorization: Bearer <worker_token>`,
return 403. Unauthenticated requests (the dashboard's normal usage)
still get the full response, consistent with the "dashboard = admin
by design" stance (ADR-0003).

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_tokens_endpoint_unauthenticated_still_returns_admin_token(
    tmp_path,
) -> None:
    """Baseline: no Authorization header → admin_token returned (preserves
    dashboard-as-admin behavior in path-prefixed deployments)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/tokens")
        assert r.status_code == 200, r.text
        assert "admin_token" in r.json()


async def test_tokens_endpoint_with_admin_bearer_returns_admin_token(
    tmp_path,
) -> None:
    """Admin Authorization header → admin_token returned."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["admin_token"] == admin.admin_token


async def test_tokens_endpoint_with_worker_bearer_returns_403(tmp_path) -> None:
    """Worker Authorization header → 403, admin_token NEVER appears in response.

    Without this, any worker token can `curl -H 'Authorization: Bearer
    <worker>' /api/tokens` and read the admin token. Issue O.
    """
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker-x")

        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 403, r.text
        body = r.text
        assert admin.admin_token not in body, (
            "worker bearer received the admin token in response body — "
            "escalation"
        )
