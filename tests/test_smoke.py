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
    """create_app() + lifespan startup + a basic GET to /api/tokens."""
    async with mcp_session(tmp_path) as admin:
        response = admin.client.get("/api/tokens")

        assert response.status_code == 200, response.text

        payload = response.json()
        assert "admin_token" in payload
        assert isinstance(payload["admin_token"], str)
        assert len(payload["admin_token"]) > 0
        assert "agent_tokens" in payload
        assert isinstance(payload["agent_tokens"], list)
