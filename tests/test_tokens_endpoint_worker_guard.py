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
        # retire-system-token Wave 3: the ``g.system_token`` god-key
        # global is gone. The leak-prevention contract is now "no
        # operator-tier credential — the harness's manager-role admin
        # token — leaks via the 401 path." The 401 body should not
        # include any agents-table tokens at all.
        assert admin.admin_token not in r.text, (
            "operator-tier admin bearer leaked in 401 response body"
        )


async def test_tokens_endpoint_with_admin_bearer_returns_200(
    tmp_path,
) -> None:
    """Admin Authorization header → 200 with ``agent_tokens`` body.

    retire-system-token Wave 1: ``admin.admin_token`` is now a real
    per-agent manager bearer (the harness seeds an admin agent row);
    the dep's ``_bearer_is_operator_tier`` check admits it. The
    response includes that token in ``agent_tokens`` (it IS a per-
    agent token after all), which is correct post-Wave-1.

    retire-system-token Wave 3: the ``g.system_token`` god-key global
    is gone. The leak-prevention check that pre-Wave-3 pinned the
    god-key not-leaking is now redundant — there is no system bearer
    to leak. We retain a sanity assertion that the body shape is
    intact.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "agent_tokens" in body


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
        # retire-system-token Wave 3: ``g.system_token`` is gone. The
        # leak-prevention contract becomes "no operator-tier credential
        # — the harness's manager-role admin token — appears in the
        # worker's 401 response body". That's the meaningful escalation
        # surface post-system-bearer-retirement.
        assert admin.admin_token not in body, (
            "worker bearer received the admin token in response body — "
            "escalation"
        )
