"""Smoke test: agent-mcp can build, start, and respond to one request.

This is the green-CI baseline. If this test breaks, the whole package
is broken in a way that no other test would even reach.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The TestClient HTTP surface is still
the simplest way to verify the basic startup path.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_app_starts(tmp_path) -> None:
    """create_app() + lifespan startup + a basic GET to /api/tokens.

    Wave 1 of prancy-napping-pie put ``/api/tokens`` behind
    ``require_operator_session``. We exercise the legacy admin-bearer
    fallback path (the dep accepts it for backwards-compat with admin
    scripts + tests like this one) so the smoke test still verifies
    end-to-end app boot + a real handler returning a real payload.

    Wave 3 (prancy-napping-pie) dropped the ``admin_token`` field
    from the response; only ``agent_tokens`` remains.
    """
    async with mcp_session(tmp_path) as admin:
        response = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )

        assert response.status_code == 200, response.text

        payload = response.json()
        assert "agent_tokens" in payload
        assert isinstance(payload["agent_tokens"], list)
