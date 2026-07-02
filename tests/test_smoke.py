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
    """create_app() + lifespan startup + a basic GET to /api/all-data.

    Wave 1 of prancy-napping-pie put the operator-gated GET routes
    behind ``require_operator_session``; the surviving auth surfaces
    are (a) operator-session cookie and (b) signed forwarding header.
    The harness's ``admin.get`` helper attaches a signed forwarding
    header on every call, so the smoke test verifies end-to-end boot
    + a real operator-gated handler.

    The probe endpoint used to be ``/api/tokens``, but the token-
    disclosure fix (2026-07) restricts that endpoint to CONFIRMED
    operator-tier bearers (a forwarding/session caller's tier is
    unverifiable in the backend and could be a viewer). ``/api/all-data``
    still returns 200 for the forwarding path, so it is the right
    boot-smoke probe now.
    """
    async with mcp_session(tmp_path) as admin:
        response = admin.get("/api/all-data")

        assert response.status_code == 200, response.text

        payload = response.json()
        assert "agents" in payload
        assert isinstance(payload["agents"], list)
