"""BL-R17-2: agent-purge must carve out terminal tasks (no resurrection).

The purge cascade (``DELETE /api/agents/{id}?cascade=true``) unassigns
every task the purged agent held. Before the fix it did so
unconditionally:

    UPDATE tasks SET assigned_to = NULL, status = 'unassigned', ...
    WHERE assigned_to = ?

so a TERMINAL task (``completed`` / ``cancelled`` / ``failed``) assigned
to the purged agent was RESURRECTED back to ``unassigned`` — and the
``reassigned_tasks`` list (which feeds the ``notify_unassigned_task_
appeared`` fanout) included it too, so a worker got woken to re-execute
already-finished work.

This is the same terminal-resurrection class as:
  * BL-R17-1  — dashboard clear-assignment (app/routers/composition.py)
  * BL-R12-1  — the terminal-sink transition guard (task_tools.py)
The canonical terminate producer (tools/admin_tools.py) already carves
out ``status NOT IN (terminal)``; purge was the missed sibling.

Fix: purge only transitions NON-terminal tasks to ``unassigned`` (and
only fans out notify for those). A terminal task assigned to the purged
agent keeps its terminal status; its ``assigned_to`` is NULLed in a
separate UPDATE (the FK ``tasks.assigned_to -> agents.agent_id`` would
otherwise block the final ``DELETE FROM agents``), but no notify fires.

RED on origin/main (terminal task -> status='unassigned' + notify =
resurrection); GREEN after the carve-out. Notify-spy pattern mirrors the
BL-R16-1 clear-assignment test.
"""

from __future__ import annotations

import datetime

import pytest

import agent_mcp.core.globals as _g_mod
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_TERMINAL = ("completed", "cancelled", "failed")


def _row(task_id: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _insert_task(
    *,
    task_id: str,
    assigned_to,
    status: str,
) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Task

    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Purge keys on assigned_to/status, not parentage.
    parent = existing_root_task_id()

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Task(
                task_id=task_id,
                title=f"task {task_id}",
                description=None,
                assigned_to=assigned_to,
                created_by="admin",
                status=status,
                priority="medium",
                created_at=now,
                updated_at=now,
                parent_task=parent,
            )
        )
        session.commit()


def _install_notify_spy(monkeypatch) -> list[tuple]:
    """Record unassigned-appeared fanouts. Installed AFTER worker setup so
    creation side effects don't pollute the recorder."""
    unassigned: list[tuple] = []
    monkeypatch.setattr(
        _g_mod, "notify_unassigned_task_appeared",
        lambda task_id: unassigned.append(task_id),
    )
    return unassigned


def _purge(admin, agent_id: str):
    return admin.request(
        "DELETE",
        f"/api/agents/{agent_id}",
        params={"cascade": "true"},
        json={},
    )


# ===================== terminal carve-out (the fix) ================== #


@pytest.mark.parametrize("terminal_status", _TERMINAL)
async def test_purge_does_not_resurrect_terminal_task(
    tmp_path, terminal_status,
) -> None:
    """Purging an agent that held a TERMINAL task must NOT flip that task
    back to 'unassigned' — its terminal status is preserved."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="term-1", assigned_to=agent_id, status=terminal_status,
        )

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        row = _row("term-1")
        assert row is not None
        assert row["status"] == terminal_status, (
            f"purging must preserve a terminal task's status; "
            f"got {row['status']!r} (resurrection)"
        )
        assert row["status"] != "unassigned", "terminal task resurrected"


@pytest.mark.parametrize("terminal_status", _TERMINAL)
async def test_purge_does_not_notify_for_terminal_task(
    tmp_path, monkeypatch, terminal_status,
) -> None:
    """No unassigned-appeared fanout for a terminal task — a worker must
    NOT be woken to re-execute already-finished work."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="term-2", assigned_to=agent_id, status=terminal_status,
        )

        unassigned = _install_notify_spy(monkeypatch)

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        fired = list(unassigned)
        assert "term-2" not in fired, (
            f"a terminal task must NOT fire notify_unassigned_task_"
            f"appeared on purge; fired={unassigned}"
        )


@pytest.mark.parametrize("terminal_status", _TERMINAL)
async def test_purge_clears_terminal_assigned_to_ref(
    tmp_path, terminal_status,
) -> None:
    """The terminal task's ``assigned_to`` must be NULLed (the agent row
    is hard-deleted; a dangling FK would block the purge). Status stays
    terminal — only the reference is cleared."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="term-3", assigned_to=agent_id, status=terminal_status,
        )

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        row = _row("term-3")
        assert row is not None
        assert row["assigned_to"] is None, (
            "purge must clear the dangling assigned_to ref"
        )
        assert row["status"] == terminal_status


# ========================= regressions ============================== #


@pytest.mark.parametrize("active_status", ["pending", "in_progress"])
async def test_purge_still_unassigns_active_task(
    tmp_path, active_status,
) -> None:
    """Regression: a NON-terminal task held by the purged agent still
    transitions to 'unassigned' and returns to the pool."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="active-1", assigned_to=agent_id, status=active_status,
        )

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        row = _row("active-1")
        assert row is not None
        assert row["assigned_to"] is None
        assert row["status"] == "unassigned", (
            f"active task must be reclaimable after purge; got {row['status']!r}"
        )


async def test_purge_still_notifies_for_active_task(
    tmp_path, monkeypatch,
) -> None:
    """Regression: an active task held by the purged agent still fires the
    unassigned fanout (with its required capabilities) so idle workers are
    woken to claim it."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="active-2", assigned_to=agent_id, status="pending",
        )

        unassigned = _install_notify_spy(monkeypatch)

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        assert "active-2" in unassigned, (
            f"active task must fire notify on purge; fired={unassigned}"
        )


async def test_purge_mixed_terminal_and_active(
    tmp_path, monkeypatch,
) -> None:
    """Mixed load: one terminal + one active task on the same purged
    agent. Terminal stays terminal (no notify); active unassigns (notify).
    Also asserts the created_by reattribution still tombstones both."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice")
        agent_id = worker.agent_id

        _insert_task(
            task_id="mix-term", assigned_to=agent_id, status="completed",
        )
        _insert_task(
            task_id="mix-active", assigned_to=agent_id, status="in_progress",
        )

        unassigned = _install_notify_spy(monkeypatch)

        resp = _purge(admin, agent_id)
        assert resp.status_code == 200, resp.text

        term = _row("mix-term")
        active = _row("mix-active")
        assert term["status"] == "completed"
        assert term["assigned_to"] is None
        assert active["status"] == "unassigned"
        assert active["assigned_to"] is None

        fired = list(unassigned)
        assert "mix-active" in fired, "active task must be fanned out"
        assert "mix-term" not in fired, "terminal task must NOT be fanned out"
