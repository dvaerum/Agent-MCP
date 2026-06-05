"""assign_task accepts agent_id as admin-only alternative to agent_token (Phase 7d).

This retires the `create_task_for` router synthetic. Single MCP call from admin
should be able to target an agent by agent_id (the human-readable name) without
the caller having to look up the agent's token first.

Behavior:
- Admin + agent_id (no agent_token): resolve agent_id → token server-side, proceed.
- Admin + unknown agent_id: clear "Unknown agent_id: '<id>'" error.
- Worker + agent_id: rejected as admin-only (workers must pass their own token).
- Admin + BOTH agent_id and agent_token: agent_token wins, agent_id ignored.

Migrated to use `tests/harness.py::mcp_session` (Candidate E from
architecture review 2026-06-01). The earlier per-test write-queue
monkeypatch was retired in PR-W1a once `execute_db_write` learned
to rebind its worker to the current event loop on every call.
"""

from __future__ import annotations

import re

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_admin_can_use_agent_id_instead_of_agent_token(tmp_path) -> None:
    """Admin passes agent_id='alice' → server resolves to alice's token,
    creates and assigns the task in one call."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await admin.assert_tool_succeeds(
            "assign_task",
            {
                "agent_id": alice.agent_id,
                "task_title": "do the thing",
                "task_description": "all of it",
            },
        )
        text = result[0].text
        assert "Error" not in text and "error" not in text, text

        m = re.search(r"task_[a-f0-9]+", text)
        assert m, f"no task_id in result: {text}"
        task_id = m.group(0)

        row = admin.task_row(task_id)
        assert row is not None, f"task {task_id} not in /api/tasks listing"
        assert row.get("assigned_to") == alice.agent_id, (
            f"expected assigned_to=={alice.agent_id}, "
            f"got {row.get('assigned_to')!r}"
        )


async def test_admin_unknown_agent_id_returns_clear_error(tmp_path) -> None:
    """Admin passes an agent_id that doesn't exist → clear error message
    naming the bad id."""
    async with mcp_session(tmp_path) as admin:
        # Not using assert_tool_succeeds — we expect an Error TextContent.
        result = await admin.call(
            "assign_task",
            {
                "agent_id": "ghost-agent",
                "task_title": "x",
                "task_description": "y",
            },
        )
        text = result[0].text
        assert "Unknown agent_id" in text and "ghost-agent" in text, text


async def test_worker_cannot_use_agent_id_admin_only(tmp_path) -> None:
    """Workers may not pass agent_id (it's an admin-only parameter)."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        # We assert against the wire-level Unauthorized + admin-only
        # hint shape; the helper handles the Unauthorized check, the
        # extra "admin-only" word is asserted manually for specificity.
        result = await bob.call(
            "assign_task",
            {
                "agent_id": bob.agent_id,
                "task_title": "x",
                "task_description": "y",
            },
        )
        text = result[0].text
        assert "Unauthorized" in text and "admin-only" in text, text


async def test_admin_both_agent_id_and_agent_token_prefers_token(tmp_path) -> None:
    """When both are provided, agent_token wins. agent_id is ignored
    silently (no error). The task ends up assigned to the agent_token's
    owner, not the agent_id's owner."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await admin.assert_tool_succeeds(
            "assign_task",
            {
                "agent_id": bob.agent_id,  # decoy — should be ignored
                "agent_token": alice.token,  # this wins
                "task_title": "do the thing",
                "task_description": "all of it",
            },
        )
        text = result[0].text
        assert "Error" not in text and "error" not in text, text

        task_id = re.search(r"task_[a-f0-9]+", text).group(0)
        row = admin.task_row(task_id)
        assert row is not None
        assert row.get("assigned_to") == alice.agent_id, (
            f"agent_token must win when both provided; "
            f"got {row.get('assigned_to')!r}"
        )
