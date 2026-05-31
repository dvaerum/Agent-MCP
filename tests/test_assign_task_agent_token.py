"""assign_task honors agent_token on create (issue L).

Per UPSTREAM_ISSUES.md issue L: calling
`assign_task(token=admin, agent_token=worker, task_title=X,
task_description=Y)` should create AND assign in one call. The
documented bug was that agent_token got ignored — task created
but assigned_to was empty.

The fix may already be in upstream (Mode 1 of assign_task sets
assigned_to = target_agent_id at line 1502). This test verifies it.

Also tests:
- issue M: assign_task moves a previously-unassigned task to
  status='pending' when it acquires assigned_to.
- The router's `create_task_for_self` and `claim_task` synthetics
  can retire once these work natively.

Migrated to use `tests/harness.py::mcp_session` (Candidate E from
architecture review 2026-06-01) — old boilerplate (`_seed_worker`,
`_admin`, `_call_assign`, `_row`) collapses into harness helpers.
Behavior is unchanged.
"""

from __future__ import annotations

import re

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_assign_task_create_and_assign_in_one_call(tmp_path) -> None:
    """Mode 1: agent_token + task_title/description → task created
    AND assigned to the agent in one call (issue L)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await admin.assert_tool_succeeds(
            "assign_task",
            {
                "agent_token": alice.token,
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
            f"got {row.get('assigned_to')!r}; issue L would manifest as "
            "empty assigned_to"
        )


async def test_assign_task_status_pending_when_assigned(tmp_path) -> None:
    """Issue M: a newly assigned task must have status 'pending', not
    'unassigned'. Otherwise the dashboard's 'Unassigned' filter shows
    the task even after assignment."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await admin.assert_tool_succeeds(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "do the thing",
                "task_description": "all of it",
            },
        )
        task_id = re.search(r"task_[a-f0-9]+", result[0].text).group(0)
        row = admin.task_row(task_id)
        assert row is not None
        assert row.get("status") in ("pending", "in_progress"), (
            f"newly-assigned task has status {row.get('status')!r}; "
            "issue M would manifest as 'unassigned'"
        )
