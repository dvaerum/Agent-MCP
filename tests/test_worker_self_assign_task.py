"""Workers may claim existing unassigned tasks via Mode 3 of
`assign_task` (agent_token=<self> + task_ids=[...]) when the
per-project policy toggle `config_allow_worker_self_assign` is on.

Background (Phase 7e). The dashboard already exposes a Settings tab
toggle called `config_allow_worker_self_assign`, but the backend
ignores it: `assign_task_tool_impl` rejects every worker token up
front via `verify_token(..., "admin")` — so even a worker calling
with their own `agent_token` to pick up an unassigned task fails
with "Unauthorized: Admin token required".

This file pins the corrected permission matrix:

- worker bearer + `agent_token=<self>` + `task_ids=[existing unassigned]`
  → allowed when toggle is on (default), rejected when toggle is off
- worker bearer + `agent_token=<other worker's token>` + `task_ids=[...]`
  → rejected regardless of toggle (workers may only self-claim)
- admin bearer + any combination → unchanged, still allowed
- Mode 0 worker-creates-unassigned (PR #32) → unchanged

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _set_toggle(key: str, value: bool) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    now_iso = _dt.datetime.now().isoformat()
    cursor.execute(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, description, created_at, created_by, "
        "updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            "true" if value else "false",
            "test toggle",
            now_iso,
            "test",
            now_iso,
            "test",
        ),
    )
    conn.commit()
    conn.close()


def _seed_unassigned_task(title: str = "needs an owner") -> str:
    """Insert a row directly so we don't depend on Mode 0 being green
    in this test file's setup. Returns the task_id."""
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "test description",
            "pending",
            "medium",
            None,
            "admin",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    return task_id


async def test_worker_self_claim_allowed_by_default(tmp_path) -> None:
    """Default (toggle absent → allow): a worker may call
    `assign_task` with their own `agent_token` and a list of existing
    unassigned task ids to claim them."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_unassigned_task("alice should pick this up")

        result = await alice.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_ids": [task_id],
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, (
            f"worker self-claim should be permitted by default; got {text!r}"
        )
        assert "Assigned" in text or task_id in text, (
            f"expected success response mentioning the task; got {text!r}"
        )

        # Verify in DB: task is now assigned to alice.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == alice.agent_id, (
            f"task should be assigned to {alice.agent_id!r}; "
            f"got {row and row['assigned_to']!r}"
        )


async def test_worker_self_claim_rejected_when_toggle_off(tmp_path) -> None:
    """Toggle off: worker self-claim must be rejected with a clear
    error message that names the policy key."""
    async with mcp_session(tmp_path) as admin:
        _set_toggle("config_allow_worker_self_assign", False)
        alice = await admin.create_worker("alice")
        task_id = _seed_unassigned_task()

        result = await alice.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_ids": [task_id],
            },
        )
        text = result[0].text
        assert "Unauthorized" in text, (
            f"toggle=off must reject worker self-claim; got {text!r}"
        )
        assert "config_allow_worker_self_assign" in text, (
            f"error should reference the policy key for discoverability; "
            f"got {text!r}"
        )


async def test_worker_cannot_assign_to_other_worker_with_task_ids(
    tmp_path,
) -> None:
    """A worker may NOT use Mode 3 to pin an existing task on a
    different worker, regardless of toggle state. The self-assign
    toggle gates self-claim only — never proxy-assignment."""
    async with mcp_session(tmp_path) as admin:
        _set_toggle("config_allow_worker_self_assign", True)
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        task_id = _seed_unassigned_task()

        result = await alice.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_ids": [task_id],
            },
        )
        text = result[0].text
        assert "Unauthorized" in text, (
            f"worker must not be able to assign tasks to other workers; "
            f"got {text!r}"
        )

        # Task must still be unassigned.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] is None, (
            f"task must remain unassigned after rejected proxy-assign; "
            f"got assigned_to={row and row['assigned_to']!r}"
        )


async def test_worker_cannot_create_and_assign_to_other_via_task_title(
    tmp_path,
) -> None:
    """Mode 1 path (no `task_ids`, but `agent_token` to someone else
    + `task_title`/`task_description`) must remain rejected — workers
    cannot create-and-assign-to-others."""
    async with mcp_session(tmp_path) as admin:
        _set_toggle("config_allow_worker_self_assign", True)
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await alice.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": "alice trying to assign-on-create to bob",
                "task_description": "should fail",
            },
        )
        text = result[0].text
        assert "Unauthorized" in text, (
            f"worker create-and-assign-to-other must be rejected; "
            f"got {text!r}"
        )


async def test_admin_self_claim_via_agent_token_unchanged(tmp_path) -> None:
    """No regression: admin token can still use Mode 3 with any
    agent_token (including a worker's) to assign existing tasks."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_unassigned_task("admin assigns to alice")

        result = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_ids": [task_id],
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == alice.agent_id


async def test_worker_create_unassigned_still_works(tmp_path) -> None:
    """Regression guard for PR #32: worker Mode 0 (no agent_token)
    creates an unassigned task — must remain unaffected by this PR."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await alice.call(
            "assign_task",
            {
                "task_title": "found a bug",
                "task_description": "needs triage",
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, (
            f"worker Mode 0 (PR #32) must still succeed; got {text!r}"
        )

        assert re.search(r"task_[a-f0-9]+", text), f"no task_id in: {text}"
