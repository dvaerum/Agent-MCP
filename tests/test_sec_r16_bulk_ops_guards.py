"""AZ-R16-1 — ``bulk_task_operations`` privilege-parity guards.

The bulk surface (``bulk_task_operations``, gated only by capability
``tasks.update``) fanned out to per-op writes WITHOUT honoring two
guards the canonical single-write path (``update_task_status`` via
``_update_single_task``) enforces:

  1. ``update_priority`` — ``priority`` is an admin/manager-only field
     on the single path (gated by ``is_admin_request =
     has_capability("tasks.assign")`` inside ``_update_single_task``).
     The bulk surface let a plain worker set ``priority``, so a worker
     could escalate its own task low→high.

  2. ``update_status`` — the single path is decorated
     ``@requires_policy("config_allow_worker_update_own_status")`` so
     that when an operator turns the toggle OFF, workers can't
     transition task status. The bulk surface ignored the toggle, so a
     worker could still update status with the toggle off.

Fix: enforce the SAME per-op guards inside the bulk loop —
``update_priority`` requires ``is_admin_request``; ``update_status``
honors ``config_allow_worker_update_own_status`` for non-admin
workers. Admin/manager callers (``tasks.assign``) retain full bulk
capability; the already-gated ``reassign`` / ``add_note`` ops are
unchanged.

These tests drive the tool as a WORKER principal (not admin) and
assert against the DB directly (authoritative source), since the
in-memory cache and the tool's text result can diverge.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_assigned_task(
    title: str, assigned_to: str, *, priority: str = "low",
    status: str = "pending",
) -> str:
    """Insert a task row assigned to ``assigned_to`` + mirror it into
    the in-memory cache. Mirrors the other bulk-ops SEC tests."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed description", status, priority,
            assigned_to, "admin", now, now, "[]",
        ),
    )
    conn.commit()
    conn.close()

    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
    }
    return task_id


def _db_task(task_id: str) -> dict:
    """Read a task row straight from the DB (authoritative source)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status, priority, notes FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} vanished"
    return dict(row)


# ==========================================================================
# RED #1 — worker cannot escalate priority via bulk update_priority
# ==========================================================================


async def test_worker_bulk_update_priority_denied(tmp_path) -> None:
    """A worker calling ``bulk_task_operations`` with an
    ``update_priority`` op on its OWN task must NOT change the priority.

    RED (origin/main): the bulk loop writes ``priority`` for any caller,
    so the worker escalates low→high. GREEN: the op is denied and the
    priority stays ``low`` (parity with ``_update_single_task``, which
    drops the priority field for a non-admin)."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        task_id = _seed_assigned_task(
            "esc target", "alice", priority="low"
        )

        result = await worker.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_priority", "task_id": task_id,
                 "priority": "high"},
            ]},
        )
        text = result[0].text

        # Authoritative source: priority unchanged.
        assert _db_task(task_id)["priority"] == "low", text
        # The op must not report a successful priority change.
        assert "priority updated to 'high'" not in text, text


# ==========================================================================
# RED #2 — worker cannot update status when the policy toggle is OFF
# ==========================================================================


async def test_worker_bulk_update_status_denied_when_policy_off(
    tmp_path,
) -> None:
    """With ``config_allow_worker_update_own_status`` disabled, a worker
    calling ``bulk_task_operations`` update_status on its OWN task must
    be denied — mirroring the single path's
    ``@requires_policy(...)`` decorator.

    RED (origin/main): the bulk surface ignores the toggle, so the
    status flips to ``in_progress``. GREEN: the op is denied and the
    status stays ``pending``."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle(
            "config_allow_worker_update_own_status", "false"
        )
        worker = await admin.create_worker("alice")
        task_id = _seed_assigned_task(
            "status target", "alice", status="pending"
        )

        result = await worker.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": task_id,
                 "status": "in_progress"},
            ]},
        )
        text = result[0].text

        assert _db_task(task_id)["status"] == "pending", text
        assert "status updated to 'in_progress'" not in text, text


# ==========================================================================
# Regression — admin/operator retains full bulk capability
# ==========================================================================


async def test_admin_bulk_update_priority_and_status_still_work(
    tmp_path,
) -> None:
    """An admin/operator (carries ``tasks.assign``) can still bulk-update
    both priority AND status — the guards only constrain non-admins."""
    async with mcp_session(tmp_path) as admin:
        task_id = _seed_assigned_task(
            "admin bulk", "admin", priority="low", status="pending"
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_priority", "task_id": task_id,
                 "priority": "high"},
                {"type": "update_status", "task_id": task_id,
                 "status": "in_progress"},
            ]},
        )
        text = result[0].text
        assert "Unauthorized" not in text, text

        row = _db_task(task_id)
        assert row["priority"] == "high", text
        assert row["status"] == "in_progress", text


async def test_admin_bulk_update_status_works_with_policy_off(
    tmp_path,
) -> None:
    """The status policy toggle only gates non-admin workers. An
    operator retains bulk status updates even with the toggle OFF
    (parity with the single path, where operator-tier bypasses the
    ``@requires_policy`` decorator)."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle(
            "config_allow_worker_update_own_status", "false"
        )
        task_id = _seed_assigned_task(
            "admin bulk toggled", "admin", status="pending"
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": task_id,
                 "status": "in_progress"},
            ]},
        )
        text = result[0].text
        assert _db_task(task_id)["status"] == "in_progress", text


# ==========================================================================
# Regression — worker CAN update its own status when the policy is ON
# ==========================================================================


async def test_worker_bulk_update_status_allowed_when_policy_on(
    tmp_path,
) -> None:
    """With the policy at its default (ON), a worker may still update its
    OWN task's status via bulk — the fix must not over-restrict."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        task_id = _seed_assigned_task(
            "worker allowed", "alice", status="pending"
        )

        result = await worker.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "update_status", "task_id": task_id,
                 "status": "in_progress"},
            ]},
        )
        text = result[0].text
        assert _db_task(task_id)["status"] == "in_progress", text


# ==========================================================================
# Regression — the already-gated ops (add_note / reassign) still behave
# ==========================================================================


async def test_worker_bulk_add_note_still_allowed(tmp_path) -> None:
    """``add_note`` is a legitimate worker op on an owned task — it must
    keep working after the fix."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        task_id = _seed_assigned_task("note target", "alice")

        result = await worker.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "add_note", "task_id": task_id,
                 "content": "worker progress note"},
            ]},
        )
        text = result[0].text
        assert "Note added" in text, text
        assert "worker progress note" in _db_task(task_id)["notes"], text


async def test_worker_bulk_reassign_still_denied(tmp_path) -> None:
    """``reassign`` remains admin-only — a worker is refused."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        task_id = _seed_assigned_task("reassign target", "alice")

        result = await worker.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id,
                 "assigned_to": "bob"},
            ]},
        )
        text = result[0].text
        assert "requires admin privileges" in text, text
        # Ownership unchanged.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT assigned_to FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["assigned_to"] == "alice", text
