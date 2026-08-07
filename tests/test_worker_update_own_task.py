"""Workers must be able to update_task_status on tasks they're
assigned to (issue N).

UPSTREAM_ISSUES.md issue N: worker calls update_task_status on a
task that belongs to them, gets back the admin-required error.
Router papers over with synthetic `update_my_task_status`.

Looking at task_tools._update_single_task lines 388-395, the
permission check is:
    if (assigned_to != requesting_agent_id) and (not is_admin):
        return unauthorized

so an assignee SHOULD be allowed. This test verifies — if it passes
without code change, issue N is also already fixed (like L + M);
the test then locks in the behavior as a regression guard.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_worker_can_update_own_task_status(tmp_path) -> None:
    """The assignee of a task can call update_task_status (issue N)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        # Admin creates + assigns a task to the worker via REST (same
        # endpoint legacy fixtures hit).
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "do thing",
                "task_description": "...",
                "assigned_to": alice.agent_id,
            },
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        # Worker updates their own task status via the MCP tool surface.
        result = await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        )
        text = result[0].text
        assert "Unauthorized" not in text, (
            f"worker can't update own task (issue N would manifest here): "
            f"{text}"
        )


async def test_worker_cannot_update_someone_elses_task(tmp_path) -> None:
    """A worker NOT assigned to a task can't update it — even with the
    issue N fix, the permission boundary stays at 'you can update
    what you own'."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # Task assigned to alice.
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": "alice's task",
                "task_description": "...",
                "assigned_to": alice.agent_id,
            },
        )
        task_id = r.json()["task_id"]

        # Bob tries to update — must fail.
        await bob.call(
            "update_task_status",
            {"task_id": task_id, "status": "completed"},
        )
        # Per-task error wrapping varies; either a top-level Unauthorized
        # or a per-task error message is acceptable. Just assert the
        # update did NOT take effect.
        row = admin.task_row(task_id)
        assert row is not None
        assert row["status"] != "completed", (
            "bob (not assigned) successfully completed alice's task — "
            "permission boundary broken"
        )
