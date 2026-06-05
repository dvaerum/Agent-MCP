"""Workers may create unassigned tasks (filing into the shared pool)
when the per-project policy toggle allows it.

Background (Q6d). The router maintains a synthetic
`create_unassigned_task` tool because upstream `assign_task` rejects
worker tokens at line ~1121: `verify_token(admin_auth_token, "admin")`
→ "Unauthorized: Admin token required". Even Mode 0 of `assign_task`
(create-with-no-`agent_token` → unassigned task) requires admin.

The synthetic exists so workers can file work they discover but
don't want to take themselves; peers then `list_unassigned_tasks`
+ `claim_task` to pick it up. Per the plan (Q6d), promote this to
a native upstream capability gated by `config_allow_worker_create_unassigned`
(default **allow**, exposed as a Settings tab toggle), then retire
the router synthetic in Phase 5.

Behavior matrix:
- admin token → always allowed (existing behavior, unchanged)
- worker token + no `agent_token` + toggle ON (default) → allowed, creates an unassigned task
- worker token + no `agent_token` + toggle OFF → rejected with a clear error message
- worker token + `agent_token` provided → still rejected (workers
  can't assign tasks to others; that's a separate plan item)

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). The earlier per-test write-queue
monkeypatch was retired in PR-W1a once `execute_db_write` learned
to rebind its worker to the current event loop on every call.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _set_toggle(value: bool) -> None:
    """Set config_allow_worker_create_unassigned in project_context."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    # project_context post-Phase-7b schema (key, value, description,
    # created_at, created_by, updated_at, updated_by)
    now_iso = _dt.datetime.now().isoformat()
    cursor.execute(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, description, created_at, created_by, "
        "updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "config_allow_worker_create_unassigned",
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


async def test_worker_can_create_unassigned_task_with_default_toggle(
    tmp_path,
) -> None:
    """Default (toggle absent → allow): a worker token may call
    `assign_task` in mode 0 (no `agent_token`) and successfully file
    a task into the unassigned pool."""
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
            f"worker token should be permitted to create unassigned tasks "
            f"by default; got {text!r}"
        )

        m = re.search(r"task_[a-f0-9]+", text)
        assert m, f"no task_id in result: {text}"
        task_id = m.group(0)

        row = admin.task_row(task_id)
        assert row is not None, f"task {task_id} not in /api/tasks listing"
        assert row.get("status") == "unassigned", (
            f"expected status='unassigned', got {row.get('status')!r}"
        )
        assert not row.get("assigned_to"), (
            f"expected no assigned_to on unassigned task, got "
            f"{row.get('assigned_to')!r}"
        )


async def test_worker_create_unassigned_blocked_when_toggle_off(
    tmp_path,
) -> None:
    """When the admin explicitly turns the toggle off, worker calls
    must be rejected with a clear error pointing at the policy."""
    async with mcp_session(tmp_path) as admin:
        _set_toggle(False)
        alice = await admin.create_worker("alice")

        result = await alice.call(
            "assign_task",
            {
                "task_title": "found a bug",
                "task_description": "needs triage",
            },
        )
        text = result[0].text
        assert (
            "Unauthorized" in text
            or "denied" in text.lower()
            or "not permitted" in text.lower()
            or "disabled" in text.lower()
        ), f"toggle=off must reject worker; got {text!r}"
        # The error should mention the policy / toggle so admin can find
        # the knob to turn it back on.
        assert (
            "config_allow_worker_create_unassigned" in text
            or "worker" in text.lower()
        ), (
            f"error should reference the policy / worker context to make "
            f"the fix discoverable; got {text!r}"
        )


async def test_admin_can_create_unassigned_regardless_of_toggle(tmp_path) -> None:
    """Admin retains existing behavior — toggle does not gate admin."""
    async with mcp_session(tmp_path) as admin:
        _set_toggle(False)

        result = await admin.call(
            "assign_task",
            {
                "task_title": "ops thing",
                "task_description": "ops desc",
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text, text
        assert re.search(r"task_[a-f0-9]+", text), f"no task_id in: {text}"


async def test_worker_cannot_assign_task_to_others(tmp_path) -> None:
    """Even with the toggle on, worker tokens may NOT use the
    agent_token field to assign work to other agents. That's a
    separate (more dangerous) capability, not part of Q6d."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await alice.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": "assign-to-bob",
                "task_description": "alice trying to assign to bob",
            },
        )
        text = result[0].text
        assert "Unauthorized" in text or "denied" in text.lower(), (
            f"worker must not be able to assign tasks to other agents; "
            f"got {text!r}"
        )
