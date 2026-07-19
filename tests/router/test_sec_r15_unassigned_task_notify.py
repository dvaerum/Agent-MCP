"""BL-R15-1: REST create of an UNASSIGNED task must fan out
``unassigned_task_appeared`` (REST-vs-MCP notify parity).

The canonical MCP unassigned-create path
(``task_tools.create_unassigned_tasks``) fires
``g.notify_unassigned_task_appeared(task_id)`` per created task so
an idle worker blocked in ``wait_for_events`` is edge-woken and a pure
GET /mcp streaming subscriber receives the push (task_tools.py:757).

The REST create handler (``tasks.create_task_api_route``, ``POST
/api/tasks``) only woke the ASSIGNEE via ``notify_agent_inbox`` in the
assigned branch. When the task was UNASSIGNED it published a
``task.created`` event under the literal id ``"*"`` — which is NOT a
wildcard wake (no agent waits under ``"*"``) — and fired NO
``unassigned_task_appeared``. So a dashboard-created unassigned task
only re-surfaced on a worker's next ~2s DB recheck, and never reached a
streaming subscriber.

This is the same notify-parity class as BL-R13-3 / BL-R14-1.

We spy ``notify_unassigned_task_appeared`` (and ``notify_agent_inbox``)
on the globals module — the contract is "the right notify fired with
the new task_id + caps", decoupled from the matcher internals.

RED on origin/main (unassigned create fires no
``notify_unassigned_task_appeared``); GREEN after the UNASSIGNED branch
mirrors the MCP path.
"""

from __future__ import annotations

import pytest

import agent_mcp.core.globals as _g_mod
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _create_task(admin, assigned_to=None) -> str:
    body = {
        "task_title": "r15-notify-target",
        "task_description": "a task created via REST",
    }
    if assigned_to is not None:
        body["assigned_to"] = assigned_to
    r = admin.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _install_spies(monkeypatch):
    """Record unassigned-appeared fanouts + inbox wakes. Installed AFTER
    setup so create_worker's own side effects don't pollute recorders."""
    unassigned: list[str] = []
    notified: list[str] = []
    monkeypatch.setattr(
        _g_mod, "notify_unassigned_task_appeared",
        lambda task_id: unassigned.append(task_id),
    )
    monkeypatch.setattr(
        _g_mod, "notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    return unassigned, notified


async def test_unassigned_create_fires_unassigned_task_appeared(
    tmp_path, monkeypatch,
) -> None:
    """Creating an UNASSIGNED task via REST must fan out
    ``unassigned_task_appeared`` for the new task_id."""
    async with mcp_session(tmp_path) as admin:
        unassigned, notified = _install_spies(monkeypatch)

        task_id = _create_task(admin)  # no assigned_to

        assert task_id in unassigned, (
            f"unassigned create must fire notify_unassigned_task_appeared "
            f"for the new task; fired={unassigned}"
        )
        # An unassigned create must NOT wake an assignee inbox (nobody is
        # assigned).
        assert notified == [], (
            f"unassigned create should wake no assignee; notified={notified}"
        )


async def test_assigned_create_wakes_inbox_not_unassigned(
    tmp_path, monkeypatch,
) -> None:
    """Regression: an ASSIGNED create still wakes the assignee's inbox
    and must NOT fire the unassigned fanout."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        unassigned, notified = _install_spies(monkeypatch)

        _create_task(admin, assigned_to="alice")

        assert "alice" in notified, (
            f"assigned create must wake the assignee inbox; notified={notified}"
        )
        assert unassigned == [], (
            f"assigned create must NOT fire the unassigned fanout; "
            f"fired={unassigned}"
        )


async def test_unassigned_create_still_returns_2xx_and_task_body(
    tmp_path,
) -> None:
    """Regression: the notify wiring must not disturb the normal
    success response — an unassigned create still returns 2xx + task."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks",
            json={"task_title": "r15-shape"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        assert body.get("task_id")
