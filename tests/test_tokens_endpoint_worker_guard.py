"""GET /api/tokens must not return admin_token to non-admin bearers.

UPSTREAM_ISSUES.md issue O (filed during this plan execution).
Pre-Wave-1 of prancy-napping-pie, `/api/tokens` returned the admin
token in plaintext to any unauthenticated HTTP caller. The original
fix returned 403 for non-admin bearers but left unauthenticated
callers admitted — relying on "the dashboard URL is the deployer's
responsibility to secure."

Wave 1 of prancy-napping-pie supersedes that: `/api/tokens` is now
behind `require_operator_session`. Any caller without one of (admin
cookie, admin bearer, body/query admin token) gets 401. The "anyone
who reaches the URL is implicitly admin" stance is gone (ADR-0003
narrower scope — the dashboard is admin, not "every byte we serve").

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_tokens_endpoint_unauthenticated_returns_401(
    tmp_path,
) -> None:
    """No Authorization header, no cookie → 401.

    Pre-Wave-1 this returned 200 + the admin_token; Wave 1 closes the
    escalation surface by gating the endpoint behind
    `require_operator_session`.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/tokens")
        assert r.status_code == 401, r.text
        assert admin.admin_token not in r.text, (
            "admin_token leaked in 401 response body"
        )


async def test_tokens_endpoint_with_admin_bearer_returns_200(
    tmp_path,
) -> None:
    """Admin Authorization header → 200 with ``agent_tokens`` body.

    The dep's legacy-bearer fallback admits admin scripts that still
    authenticate by bearer header. Wave 3 dropped the ``admin_token``
    field from the response body — see
    ``tests/test_wave3_admin_token_removal.py``.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "agent_tokens" in body
        # Wave 3: admin_token must not leak anywhere in the response.
        assert admin.admin_token not in r.text, (
            "admin token must not appear anywhere in /api/tokens response"
        )


async def test_tokens_endpoint_with_worker_bearer_returns_401(tmp_path) -> None:
    """Worker Authorization header → 401, admin_token NEVER appears in response.

    Without this, any worker token could escalate to admin by curling
    `/api/tokens`. Pre-Wave-1 the manual `verify_token(..., 'admin')`
    check returned 403; Wave 1 routes all non-admin auth attempts
    through the dep, which 401s uniformly — same security outcome,
    cleaner wire shape.
    """
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker-x")

        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {worker.token}"},
        )
        assert r.status_code == 401, r.text
        body = r.text
        assert admin.admin_token not in body, (
            "worker bearer received the admin token in response body — "
            "escalation"
        )
