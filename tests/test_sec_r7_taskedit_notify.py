"""BL-R7-1: dashboard task-edit must publish ``task.updated`` + wake inbox.

The dashboard Edit-modal handler
(``composition.update_task_details_api_route``, ``POST
/api/update-task-dashboard``) mutates fields through
``task_repo.update_fields(task_id, fields, connection=cursor)``. The
``connection=`` path DELIBERATELY defers the cache-write AND the
EventBus publish to the caller (see ``task_repository.py`` /
``task_repo.update_fields`` docstring: a subscriber must never observe
an uncommitted row). Every OTHER task-mutation path reconciles both
after commit:

  * REST create (``tasks.py``) publishes ``task.created`` +
    ``notify_agent_inbox`` for the assignee (round-5 BL-1).
  * MCP ``update_task_status`` wakes each touched task's assignee
    (``task_tools.py`` ``g.notify_agent_inbox(assignee)``).

Before this fix the dashboard edit path did NEITHER: reassigning or
re-statusing a task via the Edit modal left an agent blocked in
``wait_for_events`` unaware, and fanned no ``resources/updated`` to
subscribed ``/mcp`` sessions. These tests pin the mirror: after a
successful commit the handler publishes ``task.updated`` and wakes the
(new) assignee's inbox, and on reassignment wakes BOTH the new and the
prior assignee.

We spy the EventBus shim + ``notify_agent_inbox`` rather than drive a
real waiter — the contract is "publish + notify fired with the right
recipients", decoupled from the bus/matcher internals.
"""

from __future__ import annotations

import pytest

import agent_mcp.core.globals as _g_mod
from agent_mcp.core.repositories import _event_bus_shim as _shim
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _create_task(admin, assigned_to=None) -> str:
    body = {
        "token": admin.admin_token,
        "task_title": "r7-edit-target",
        "task_description": "a task edited via the dashboard modal",
    }
    if assigned_to is not None:
        body["assigned_to"] = assigned_to
    r = admin.client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _install_spies(monkeypatch):
    """Record EventBus publishes + inbox wakes. Installed AFTER setup so
    create/create_worker's own publishes don't pollute the recorders."""
    published: list[tuple] = []
    notified: list[str] = []
    monkeypatch.setattr(
        _shim, "publish",
        lambda agent_id, event_type, payload: published.append(
            (agent_id, event_type, payload)
        ),
    )
    monkeypatch.setattr(
        _g_mod, "notify_agent_inbox",
        lambda agent_id: notified.append(agent_id),
    )
    return published, notified


async def test_edit_assign_publishes_task_updated_and_wakes_new_assignee(
    tmp_path, monkeypatch,
) -> None:
    """Assigning + re-statusing an unassigned task via the modal must
    publish ``task.updated`` and wake the new assignee's inbox."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("agent-a")
        task_id = _create_task(admin)  # unassigned

        published, notified = _install_spies(monkeypatch)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "status": "in_progress",
                "assigned_to": "agent-a",
            },
        )
        assert r.status_code == 200, r.text

        updated = [p for p in published if p[1] == "task.updated"]
        assert updated, f"no task.updated published; got {published}"
        agent_id, _event, payload = updated[0]
        assert agent_id == "agent-a"
        assert payload.get("task_id") == task_id

        assert "agent-a" in notified, (
            f"new assignee inbox not woken; notified={notified}"
        )


async def test_reassignment_wakes_both_new_and_prior_assignee(
    tmp_path, monkeypatch,
) -> None:
    """Reassigning a task from agent-a to agent-b must wake BOTH: the
    new assignee (task entered their queue) and the prior assignee (task
    left their queue)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("agent-a")
        await admin.create_worker("agent-b")
        task_id = _create_task(admin, assigned_to="agent-a")

        published, notified = _install_spies(monkeypatch)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "assigned_to": "agent-b",
            },
        )
        assert r.status_code == 200, r.text

        assert any(p[1] == "task.updated" for p in published), (
            f"no task.updated published; got {published}"
        )
        assert "agent-b" in notified, (
            f"new assignee not woken; notified={notified}"
        )
        assert "agent-a" in notified, (
            f"prior assignee not woken on reassignment; notified={notified}"
        )


async def test_unassign_wakes_prior_assignee(tmp_path, monkeypatch) -> None:
    """Clearing the assignment must wake the prior assignee (task left
    their queue) and publish a broadcast ``task.updated``."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("agent-a")
        task_id = _create_task(admin, assigned_to="agent-a")

        published, notified = _install_spies(monkeypatch)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "assigned_to": None,
            },
        )
        assert r.status_code == 200, r.text

        updated = [p for p in published if p[1] == "task.updated"]
        assert updated, f"no task.updated published; got {published}"
        # No current assignee => broadcast target.
        assert updated[0][0] == "*"
        assert "agent-a" in notified, (
            f"prior assignee not woken on unassign; notified={notified}"
        )


async def test_noop_field_edit_still_publishes_no_assignee_change(
    tmp_path, monkeypatch,
) -> None:
    """Regression: a non-assignment edit (title-only on an unassigned
    task) still succeeds and publishes a broadcast ``task.updated`` with
    no inbox wake (no assignee to notify)."""
    async with mcp_session(tmp_path) as admin:
        task_id = _create_task(admin)  # unassigned

        published, notified = _install_spies(monkeypatch)

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={
                "token": admin.admin_token,
                "task_id": task_id,
                "title": "renamed via modal",
            },
        )
        assert r.status_code == 200, r.text

        updated = [p for p in published if p[1] == "task.updated"]
        assert updated, f"no task.updated published; got {published}"
        assert updated[0][0] == "*"
        assert notified == [], (
            f"unassigned title edit should wake nobody; notified={notified}"
        )
