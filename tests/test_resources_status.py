"""Tests for the status MCP resource (plan Phase 3).

Per `/home/dennis/.claude/plans/prancy-napping-pie.md` Phase 3:

* `agent-mcp://status/<agent_id>` exposes ambient counters as JSON:
  `{"unread_messages": N, "unfinished_tasks": M, ...}`.
* Counters reflect the caller's current world. Updated by the same
  signal hook that wakes `wait_for_events`, though the
  notification-emission side is DEFERRED (see PR body).
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
import pytest

from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


async def _read_resource(session, uri: str) -> mcp_types.ReadResourceResult:
    from pydantic_core import Url

    from agent_mcp.tools.registry import request_auth_token

    handler = session._admin._mcp_app_instance().request_handlers[
        mcp_types.ReadResourceRequest
    ]
    req = mcp_types.ReadResourceRequest(
        method="resources/read",
        params=mcp_types.ReadResourceRequestParams(uri=Url(uri)),
    )
    tok = request_auth_token.set(session.token)
    try:
        result = await handler(req)
    finally:
        request_auth_token.reset(tok)
    inner = result.root if hasattr(result, "root") else result
    return inner


def _first_text(contents) -> str:
    for c in contents:
        text = getattr(c, "text", None)
        if isinstance(text, str):
            return text
    return ""


async def test_status_returns_zero_counters_for_fresh_agent(
    tmp_path: Path,
) -> None:
    """A freshly-created worker with no messages or tasks reads
    `{unread_messages: 0, unfinished_tasks: 0}`."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        result = await _read_resource(alice, "agent-mcp://status/alice")
        text = _first_text(result.contents)
        payload = json.loads(text)
        assert payload.get("unread_messages") == 0, (
            f"want 0 unread; got {payload}"
        )
        assert payload.get("unfinished_tasks") == 0, (
            f"want 0 unfinished tasks; got {payload}"
        )


async def test_status_unread_counter_increments_after_message(
    tmp_path: Path,
) -> None:
    """After admin sends a message to alice, alice's
    `unread_messages` counter goes from 0 → 1."""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # Pre-state.
        r0 = await _read_resource(alice, "agent-mcp://status/alice")
        p0 = json.loads(_first_text(r0.contents))
        assert p0["unread_messages"] == 0

        # Send.
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "ping",
                    "deliver_method": "store",
                }
            )

        r1 = await _read_resource(alice, "agent-mcp://status/alice")
        p1 = json.loads(_first_text(r1.contents))
        assert p1["unread_messages"] == 1, (
            f"unread counter did not advance; got {p1}"
        )


async def test_status_unfinished_tasks_counter_reflects_assignments(
    tmp_path: Path,
) -> None:
    """After admin assigns a task to alice, her
    `unfinished_tasks` counter goes 0 → 1."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        r0 = await _read_resource(alice, "agent-mcp://status/alice")
        p0 = json.loads(_first_text(r0.contents))
        assert p0["unfinished_tasks"] == 0

        with with_bearer(admin.admin_token):
            await assign_task_tool_impl(
                {
                    "token": admin.admin_token,
                    "agent_token": alice.token,
                    "task_title": "Read a book",
                    "task_description": "A short one.",
                    "priority": "low",
                }
            )

        r1 = await _read_resource(alice, "agent-mcp://status/alice")
        p1 = json.loads(_first_text(r1.contents))
        assert p1["unfinished_tasks"] == 1, (
            f"unfinished_tasks did not advance; got {p1}"
        )
