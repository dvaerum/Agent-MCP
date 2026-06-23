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
    ``require_operator_session``. retire-system-token Wave 1 removed
    the legacy admin-bearer fallback from that dep; the surviving
    auth surfaces are (a) operator-session cookie and (b) signed
    forwarding header. The harness's ``admin.get`` helper attaches a
    signed forwarding header on every call, so the smoke test still
    verifies end-to-end boot + a real handler.

    Wave 3 (prancy-napping-pie) dropped the ``admin_token`` field
    from the response; only ``agent_tokens`` remains.
    """
    async with mcp_session(tmp_path) as admin:
        response = admin.get("/api/tokens")

        assert response.status_code == 200, response.text

        payload = response.json()
        assert "agent_tokens" in payload
        assert isinstance(payload["agent_tokens"], list)
