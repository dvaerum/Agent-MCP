"""BL-R13-3: dashboard message to a ghost recipient must 404, not 500.

The canonical MCP send path (``send_agent_message_tool_impl``) catches
the ``LookupError`` that ``message_repo.send`` raises for an unknown
recipient and returns a clean ``NotFound`` (HTTP 404). The dashboard
``create_message_api_route`` only caught ``ValueError`` (→ 400) and the
generic ``Exception`` (→ 500), so the same ``LookupError`` surfaced as an
uncaught 500.

RED on origin/main (500); GREEN after the route mirrors the MCP
contract (404 for a nonexistent/terminated recipient).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_dashboard_message_to_ghost_recipient_404(tmp_path) -> None:
    """Sending to a recipient that does not exist must 404, not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages",
            json={
                "recipient_id": "ghost-does-not-exist",
                "message_content": "hello nobody",
            },
        )
        assert resp.status_code == 404, (
            f"message to a nonexistent recipient must be 404 (matching the "
            f"MCP send path), got {resp.status_code}: {resp.text}"
        )


async def test_dashboard_message_to_valid_recipient_succeeds(tmp_path) -> None:
    """Regression: a message to a live recipient still succeeds."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "hello alice",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True, resp.text


async def test_dashboard_message_to_admin_label_succeeds(tmp_path) -> None:
    """Regression: the special 'admin' recipient label is a valid
    destination (no agents-table parent row post-Wave-4)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Worker -> admin escalation is always permitted.
        worker = await admin.create_worker("bob")  # noqa: F841
        resp = admin.post(
            "/api/messages",
            json={
                "recipient_id": "admin",
                "message_content": "escalation",
            },
        )
        assert resp.status_code == 200, resp.text
