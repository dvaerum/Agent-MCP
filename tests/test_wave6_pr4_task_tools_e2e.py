"""Wave 6 PR 4 — E2E coverage of task_tools migrated to
Principal + ToolResult.

Pins the new dispatch contract end-to-end for every tool in
``agent_mcp/tools/task_tools.py`` after the migration:

* The registered MCP path (``admin.call`` / ``worker.call``):
  the bridge derives a Principal from ContextVars, the migrated
  impl returns a typed :data:`ToolResult` variant, and the
  renderer turns it back into ``list[TextContent]`` the wire sees.
* The direct dispatcher path (``dispatch_tool_call`` with explicit
  ``principal=``): assert on the typed variant shape directly,
  matching ``test_wave6_pr0_e2e``'s
  ``test_add_task_note_via_dispatch_returns_ok_with_data`` pattern.

Reference for the migration pattern: ``tests/test_wave6_pr0_e2e.py``.
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Invalid,
    NotFound,
    Ok,
)
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ── Seed helpers ─────────────────────────────────────────────────


def _seed_unassigned_task(title: str = "needs an owner") -> str:
    """Insert an unassigned task row directly.

    Mirrors ``tests/test_worker_self_assign_task.py::_seed_unassigned_task``;
    repeated here so this file is self-contained.
    """
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
            "seed description",
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


def _seed_assigned_task(
    title: str, assigned_to: str, parent: str | None = None,
) -> str:
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.core import globals as g

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, parent_task, "
        "child_tasks, depends_on_tasks, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            "seed description",
            "pending",
            "medium",
            assigned_to,
            "admin",
            now,
            now,
            parent,
            "[]",
            "[]",
            "[]",
        ),
    )
    conn.commit()
    conn.close()
    # Also mirror into in-memory cache so view/search hit it.
    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "description": "seed description",
        "status": "pending",
        "priority": "medium",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "parent_task": parent,
        "child_tasks": [],
        "depends_on_tasks": [],
        "notes": [],
    }
    return task_id


def _admin_principal() -> Principal:
    """Operator-session-style Principal for explicit-dispatch tests."""
    return Principal(
        kind="operator_session",
        user_id="test-harness-operator",
        agent_id=None,
        sysadmin=False,
        project_name="demo",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── 1. assign_task admin create+assign returns Ok ────────────────


async def test_assign_task_admin_creates_and_assigns_returns_ok(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        result = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_title": "do the work",
                "task_description": "of the work",
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text
        m = re.search(r"task_[a-f0-9]+", text)
        assert m, f"no task_id in result: {text}"
        task_id = m.group(0)

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == alice.agent_id


# ── 2. assign_task Mode 3 (existing unassigned) ──────────────────


async def test_assign_task_existing_unassigned_mode3(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_unassigned_task("alice should pick this up")

        result = await admin.call(
            "assign_task",
            {
                "agent_token": alice.token,
                "task_ids": [task_id],
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text
        assert "Assigned" in text or task_id in text, text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == alice.agent_id


# ── 3. assign_task reassignment via update_task_status ───────────


async def test_assign_task_handoff_reassignment(tmp_path) -> None:
    """Admin reassigns a task via update_task_status(assigned_to=...).

    Pins the admin-only-field path of update_task_status. After
    migration, ``is_admin_request`` admits the admin harness
    (manager-role bearer + op_session) so ``new_assigned_to`` is
    applied.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        task_id = _seed_assigned_task("originally alice's", alice.agent_id)

        result = await admin.call(
            "update_task_status",
            {
                "task_id": task_id,
                "status": "in_progress",
                "assigned_to": bob.agent_id,
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Failed" not in text, text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT assigned_to FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["assigned_to"] == bob.agent_id, (
            f"expected reassignment to {bob.agent_id}; got {row and row['assigned_to']!r}"
        )


# ── 4. create_self_task worker succeeds ──────────────────────────


async def test_create_self_task_worker_succeeds(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # AZ-R19-1: a worker may only self-task under a parent it OWNS.
        # Seed the parent assigned to alice so this test exercises the
        # self-task creation path, not the ownership gate.
        root = _seed_assigned_task("root task", alice.agent_id)

        result = await alice.call(
            "create_self_task",
            {
                "task_title": "alice's subtask",
                "task_description": "a subtask alice files for herself",
                "parent_task_id": root,
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text
        assert "Self-assigned task" in text, text


async def test_create_self_task_never_carries_required_capabilities(
    tmp_path,
) -> None:
    """arch-deepening R4 #7 — locked decision, made explicit.

    ``create_self_task`` never tagged its row with
    ``required_capabilities``; before this PR that was an accidental
    omission (the field was simply missing from the ``task_repo.create``
    call dict, one of the ~7 near-identical call sites that had drifted
    from each other). The behavior is now an explicit
    ``"required_capabilities": None`` with a comment recording why: a
    self-task is always immediately self-assigned at creation, so the
    capability-routing gate ``required_capabilities`` exists to enforce
    on a SEPARATE assignment step never applies here. This test pins
    that the row lands with no capability tag regardless of what the
    caller passes (the tool schema doesn't even accept the field) —
    behavior-preserving, now on purpose.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        root = _seed_assigned_task("root task", alice.agent_id)

        result = await alice.call(
            "create_self_task",
            {
                "task_title": "alice's capability-free subtask",
                "task_description": "must not carry a capability tag",
                "parent_task_id": root,
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text

        m = re.search(r"task_[a-f0-9]+", text)
        assert m, f"no task_id in result: {text}"
        task_id = m.group(0)

        row = admin.task_row(task_id)
        assert row is not None, f"task {task_id} not in /api/tasks listing"
        assert not row.get("required_capabilities"), (
            f"create_self_task must not tag the row with "
            f"required_capabilities; got {row.get('required_capabilities')!r}"
        )


# ── 5. update_task_status worker on own task ─────────────────────


async def test_update_task_status_worker_own_task_returns_ok(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        task_id = _seed_assigned_task("alice's task", alice.agent_id)

        result = await alice.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        )
        text = result[0].text
        assert "Unauthorized" not in text, text
        assert "in_progress" in text or "updated" in text.lower(), text


# ── 6. update_task_status worker on other's task → PermissionDenied
#     (rendered text starts with "Unauthorized:") ──────────────────


async def test_update_task_status_worker_other_task_returns_not_found(
    tmp_path,
) -> None:
    """PF-1 (round 4): a worker updating a foreign task gets a
    :class:`NotFound` identical to a nonexistent task — no 403-vs-404
    existence oracle, no owner-id leak. (Previously PermissionDenied
    rendered as "Unauthorized: … assigned to <owner>".)"""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        task_id = _seed_assigned_task("alice's task", alice.agent_id)

        result = await bob.call(
            "update_task_status",
            {"task_id": task_id, "status": "completed"},
        )
        text = result[0].text
        # Renderer for NotFound: "Error: task '<id>' not found."
        assert "not found" in text.lower(), (
            f"expected NotFound rendered as '... not found.', got: {text!r}"
        )
        assert "unauthorized" not in text.lower(), text
        assert alice.agent_id not in text, (
            f"owner id must not leak; got: {text!r}"
        )

        # Same response as a genuinely nonexistent task (no differential).
        missing = await bob.call(
            "update_task_status",
            {"task_id": "no-such-task-xyz", "status": "completed"},
        )
        assert missing[0].text.replace("no-such-task-xyz", "X") == \
            text.replace(task_id, "X"), (
            f"foreign vs missing differ: {text!r} vs {missing[0].text!r}"
        )

        # And the underlying task is unchanged.
        row = admin.task_row(task_id)
        assert row is not None and row["status"] != "completed"


# ── 7. view_tasks admin returns Ok ───────────────────────────────


async def test_view_tasks_admin_returns_ok(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed_assigned_task("a", "admin")
        _seed_assigned_task("b", "admin")

        result = await admin.call("view_tasks", {})
        text = result[0].text
        assert "Unauthorized" not in text, text
        assert "Tasks" in text or "No tasks" in text, text


# ── 8. view_tasks worker filtering other agent → PermissionDenied
# ─────────────────────────────────────────────────────────────────


async def test_view_tasks_worker_other_agent_filter_permission_denied(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        # Alice tries to filter view_tasks by bob's agent_id — rejected.
        result = await alice.call("view_tasks", {"agent_id": bob.agent_id})
        text = result[0].text
        assert text.startswith("Unauthorized:"), (
            f"expected PermissionDenied rendered as 'Unauthorized: ...', "
            f"got: {text!r}"
        )


# ── 9. search_tasks with query returns Ok ────────────────────────


async def test_search_tasks_with_query_returns_ok(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        # Seed tasks the admin can search.
        from agent_mcp.core import globals as g

        now = _dt.datetime.now().isoformat()
        task_id = f"task_{secrets.token_hex(6)}"
        g.tasks[task_id] = {
            "task_id": task_id,
            "title": "investigate unique-marker xyzzy bug",
            "description": "a task with a marker word to find",
            "status": "pending",
            "priority": "high",
            "assigned_to": None,
            "created_by": "admin",
            "created_at": now,
            "updated_at": now,
            "parent_task": None,
            "child_tasks": [],
            "depends_on_tasks": [],
            "notes": [],
        }

        result = await admin.call(
            "search_tasks", {"search_query": "xyzzy bug"},
        )
        text = result[0].text
        assert "Unauthorized" not in text, text
        assert task_id in text, text


# ── 10. delete_task admin succeeds ───────────────────────────────


async def test_delete_task_admin_succeeds(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_assigned_task("to be deleted", "admin")

        result = await admin.call(
            "delete_task", {"task_id": task_id, "force_delete": True},
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Failed" not in text, text
        assert "deleted successfully" in text, text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is None, "task row should be gone after delete"


# ── 11. delete_task with children no force → Conflict ────────────


async def test_delete_task_with_children_no_force_returns_conflict(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        parent_id = _seed_assigned_task("parent", "admin")
        # Direct DB-level append to parent.child_tasks so the
        # cascade-check loads a non-empty list.
        from agent_mcp.db.connection import get_db_connection
        import json as _json

        child_id = _seed_assigned_task("child", "admin", parent=parent_id)

        conn = get_db_connection()
        conn.execute(
            "UPDATE tasks SET child_tasks = ? WHERE task_id = ?",
            (_json.dumps([child_id]), parent_id),
        )
        conn.commit()
        conn.close()

        result = await admin.call("delete_task", {"task_id": parent_id})
        text = result[0].text
        # Renderer for Conflict: "Error: conflict: <reason>".
        assert text.startswith("Error: conflict:"), (
            f"expected Conflict rendered as 'Error: conflict: ...', "
            f"got: {text!r}"
        )
        assert "force_delete" in text, text


# ── 12. request_assistance worker creates child task ─────────────


async def test_request_assistance_worker_creates_child_task(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        parent_id = _seed_assigned_task("alice's parent task", alice.agent_id)

        result = await alice.call(
            "request_assistance",
            {
                "task_id": parent_id,
                "description": "I need help with this part",
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text
        assert "Assistance requested" in text, text

        # A child task was created.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE parent_task = ?", (parent_id,)
        ).fetchall()
        conn.close()
        assert len(rows) >= 1, "expected at least one child assistance task"


# ── 13. bulk_task_operations admin update_status ─────────────────


async def test_bulk_task_operations_admin_succeeds(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_assigned_task("bulk target", "admin")

        result = await admin.call(
            "bulk_task_operations",
            {
                "operations": [
                    {
                        "type": "update_status",
                        "task_id": task_id,
                        "status": "in_progress",
                    },
                ],
            },
        )
        text = result[0].text
        assert "Unauthorized" not in text and "Error" not in text, text
        assert "in_progress" in text, text

        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row["status"] == "in_progress"


# ── Direct-dispatch shape assertions (Ok / Invalid / NotFound) ───
#
# Exercise the typed-variant return shape directly through
# ``dispatch_tool_call`` with an explicit ``principal=`` (matches the
# pattern in ``test_wave6_pr0_e2e.test_add_task_note_via_dispatch_returns_ok_with_data``).


def _agent_bearer_principal(agent_id: str, token: str) -> Principal:
    """Manager-role agent_bearer principal — mirrors the admin harness."""
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="manager",
        can_wake_loop=False,
        source_token=token,
    )


async def test_view_tasks_via_dispatch_returns_ok(tmp_path) -> None:
    """Drive ``view_tasks`` through ``dispatch_tool_call`` with an
    explicit agent_bearer Principal; assert the typed-variant return
    shape directly (matches the demo pattern in
    ``test_wave6_pr0_e2e.test_add_task_note_via_dispatch_returns_ok_with_data``).
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        result = await dispatch_tool_call(
            "view_tasks",
            {"token": admin.admin_token},
            principal=_agent_bearer_principal(
                admin.agent_id, admin.admin_token,
            ),
        )
        assert isinstance(result, Ok), f"expected Ok, got {result!r}"


async def test_update_task_status_invalid_missing_task_id_returns_invalid(tmp_path) -> None:
    """Schema doesn't require task_id (callers may pass task_ids
    instead); the tool's own ``Invalid`` rejection fires when both
    are absent. Pins the typed-variant return shape directly."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        result = await dispatch_tool_call(
            "update_task_status",
            {
                "token": admin.admin_token,
                "status": "pending",
            },
            principal=_agent_bearer_principal(
                admin.agent_id, admin.admin_token,
            ),
        )
        assert isinstance(result, Invalid), (
            f"expected Invalid for missing task_id/task_ids, got {result!r}"
        )


async def test_delete_task_not_found_returns_not_found_variant(tmp_path) -> None:
    """``delete_task`` is ``@requires_role("operator")``, so the
    decorator admits operator-session principals via the
    ``operator_session_active`` ContextVar. The harness leaves
    op_session=True for the session lifetime, so the dispatcher's
    decorator path admits and the tool's NotFound return surfaces.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "delete_task",
            {"task_id": "task_does_not_exist"},
            principal=_admin_principal(),
        )
        assert isinstance(result, NotFound), (
            f"expected NotFound, got {result!r}"
        )
        assert result.resource == "task"
        assert result.identifier == "task_does_not_exist"
