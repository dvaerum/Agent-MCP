"""Smoke test: agent-mcp can build, start, and respond to one request.

This is the green-CI baseline. If this test breaks, the whole package
is broken in a way that no other test would even reach.
"""

from __future__ import annotations


def test_app_starts(client) -> None:
    """create_app() + lifespan startup + a basic GET to /api/tokens."""
    response = client.get("/api/tokens")

    assert response.status_code == 200, response.text

    payload = response.json()
    assert "admin_token" in payload
    assert isinstance(payload["admin_token"], str)
    assert len(payload["admin_token"]) > 0
    assert "agent_tokens" in payload
    assert isinstance(payload["agent_tokens"], list)
