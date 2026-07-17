"""BL-R16-1: dashboard clear-assignment must reach unassign parity.

The dashboard task edit (``POST /api/update-task-dashboard``) supports
clearing a task's assignment (``assigned_to`` set to null / '' /
'unassigned'). Before the fix it wrote ``assigned_to = NULL`` but left
``status`` as ``pending`` / ``in_progress`` and never fired
``g.notify_unassigned_task_appeared`` — so the task never actually
became a first-class ``unassigned`` task and idle/streaming workers were
never edge-woken that it became claimable.

The three canonical unassign producers all set ``status='unassigned'``
AND fire the notify:

  * agent-terminate  (tools/admin_tools.py)
  * agent-purge      (app/routers/agents.py)
  * REST create-unassigned notify parity, BL-R15-1 (app/routers/tasks.py)

This is the last uncovered producer in the same notify-parity class as
BL-R13-3 / BL-R14-1 / BL-R15-1.

We spy ``notify_unassigned_task_appeared`` on the globals module — the
contract is "the right notify fired with the cleared task_id + caps",
decoupled from the matcher internals — mirroring the R15 test.

RED on origin/main (status stays ``pending``, no unassigned fanout);
GREEN after the clear branch mirrors the canonical unassign.
"""

from __future__ import annotations

import pytest

import agent_mcp.core.globals as _g_mod
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _grant_caps(agent_id: str, caps: list[str]) -> None:
    """Give an existing agent the capability set ``caps``.

    AZ-R26-1 gates create-with-``assigned_to`` on
    ``required_capabilities ⊆ agent.capabilities`` (parity with every
    reassign path). This test assigns a caps-tagged task to ``alice`` at
    create time, so ``alice`` must actually carry those caps — the caps
    tag is what the test asserts is *forwarded*, not that under-capable
    assignment is permitted.
    """
    import json as _json

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET capabilities = ? WHERE agent_id = ?",
            (_json.dumps(caps), agent_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _create_task(admin, **body_extra) -> str:
    body = {"task_title": "r16-clear-probe"}
    body.update(body_extra)
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _install_spies(monkeypatch):
    """Record unassigned-appeared fanouts + inbox wakes. Installed AFTER
    setup so worker creation side effects don't pollute recorders."""
    unassigned: list[tuple] = []
    notified: list[str] = []
    monkeypatch.setattr(
        _g_mod, "notify_unassigned_task_appeared",
        lambda task_id, caps: unassigned.append((task_id, caps)),
    )
    monkeypatch.setattr(
        _g_mod, "notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    return unassigned, notified


def _clear_assignment(admin, task_id: str, value):
    return admin.post(
        "/api/update-task-dashboard",
        json={
            "task_id": task_id,
            "assigned_to": value,
        },
    )


# ===================== status -> 'unassigned' ========================= #


@pytest.mark.parametrize("clear_value", [None, "", "unassigned"])
async def test_clear_assignment_transitions_status_to_unassigned(
    tmp_path, clear_value,
) -> None:
    """Clearing an assigned task via the dashboard must flip its status
    to ``unassigned`` — matching the canonical unassign producers."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        assert _row("tasks", "task_id = ?", (task_id,))["status"] == "pending"

        r = _clear_assignment(admin, task_id, clear_value)
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] is None, "assignment must be cleared"
        assert row["status"] == "unassigned", (
            f"cleared task must become 'unassigned', got {row['status']!r}"
        )


# ======================= notify parity ================================ #


async def test_clear_assignment_fires_unassigned_task_appeared(
    tmp_path, monkeypatch,
) -> None:
    """Clearing an assignment must fan out ``unassigned_task_appeared``
    for the cleared task so idle / streaming workers are edge-woken."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")

        unassigned, _notified = _install_spies(monkeypatch)

        r = _clear_assignment(admin, task_id, None)
        assert r.status_code == 200, r.text

        fired_ids = [u[0] for u in unassigned]
        assert task_id in fired_ids, (
            f"clearing an assignment must fire "
            f"notify_unassigned_task_appeared for the task; fired={unassigned}"
        )


async def test_clear_assignment_forwards_required_capabilities(
    tmp_path, monkeypatch,
) -> None:
    """The fanout must carry the task's required capabilities so only
    the qualifying agents are woken."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # AZ-R26-1: alice must carry the caps the task is tagged with to
        # be a valid assignee at create time.
        _grant_caps("alice", ["python", "docker"])
        task_id = await _create_task(
            admin, assigned_to="alice",
            required_capabilities=["python", "docker"],
        )

        unassigned, _notified = _install_spies(monkeypatch)

        r = _clear_assignment(admin, task_id, "unassigned")
        assert r.status_code == 200, r.text

        matched = [u for u in unassigned if u[0] == task_id]
        assert matched, f"no fanout for the cleared task; fired={unassigned}"
        caps = matched[0][1]
        assert set(caps) == {"python", "docker"}, (
            f"fanout must carry the task's required capabilities; got {caps}"
        )


# ========================= regressions ================================ #


async def test_reassign_to_real_agent_does_not_fire_unassigned(
    tmp_path, monkeypatch,
) -> None:
    """Reassigning to a REAL agent still sets ``assigned_to`` and must
    NOT fire the unassigned fanout nor flip status to unassigned."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        task_id = await _create_task(admin, assigned_to="alice")

        unassigned, notified = _install_spies(monkeypatch)

        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "assigned_to": "bob",
            },
        )
        assert r.status_code == 200, r.text

        row = _row("tasks", "task_id = ?", (task_id,))
        assert row["assigned_to"] == "bob", "reassignment must land"
        assert row["status"] != "unassigned", (
            "reassigning to a real agent must NOT flip status to unassigned"
        )
        assert unassigned == [], (
            f"reassignment must NOT fire the unassigned fanout; "
            f"fired={unassigned}"
        )
        # bob (new) and alice (prior) inboxes are woken; that's the
        # existing reassign behavior and must be preserved.
        assert "bob" in notified, "new assignee inbox must be woken"


async def test_normal_status_edit_still_succeeds(tmp_path) -> None:
    """A plain status edit (no assignment change) still works and does
    NOT get force-flipped to unassigned."""
    async with mcp_session(tmp_path) as admin:
        task_id = await _create_task(admin)
        r = admin.post(
            "/api/update-task-dashboard",
            json={
                "task_id": task_id,
                "status": "in_progress",
            },
        )
        assert r.status_code == 200, r.text
        assert _row("tasks", "task_id = ?", (task_id,))["status"] == (
            "in_progress"
        )


async def test_clear_assignment_still_returns_2xx(tmp_path) -> None:
    """Regression: the notify wiring must not disturb the success
    response — clearing an assignment still returns 2xx."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        task_id = await _create_task(admin, assigned_to="alice")
        r = _clear_assignment(admin, task_id, None)
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True
