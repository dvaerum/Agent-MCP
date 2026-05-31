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
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import secrets


def _seed_worker(name: str = "alice"):
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token, name


def _admin(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _call_update(arguments: dict):
    from agent_mcp.tools.task_tools import update_task_status_tool_impl

    return asyncio.run(update_task_status_tool_impl(arguments))


def test_worker_can_update_own_task_status(client) -> None:
    """The assignee of a task can call update_task_status (issue N)."""
    admin = _admin(client)
    worker_token, worker_id = _seed_worker("alice")

    # Admin creates + assigns a task to the worker.
    r = client.post(
        "/api/tasks",
        json={
            "token": admin,
            "task_title": "do thing",
            "task_description": "...",
            "assigned_to": worker_id,
        },
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    # Worker updates their own task status.
    result = _call_update({
        "token": worker_token,
        "task_id": task_id,
        "status": "in_progress",
    })
    text = result[0].text
    assert "Unauthorized" not in text, (
        f"worker can't update own task (issue N would manifest here): {text}"
    )


def test_worker_cannot_update_someone_elses_task(client) -> None:
    """A worker NOT assigned to a task can't update it — even with the
    issue N fix, the permission boundary stays at 'you can update
    what you own'."""
    admin = _admin(client)
    alice_token, alice_id = _seed_worker("alice")
    bob_token, _ = _seed_worker("bob")

    # Task assigned to alice.
    r = client.post(
        "/api/tasks",
        json={
            "token": admin,
            "task_title": "alice's task",
            "task_description": "...",
            "assigned_to": alice_id,
        },
    )
    task_id = r.json()["task_id"]

    # Bob tries to update — must fail.
    result = _call_update({
        "token": bob_token,
        "task_id": task_id,
        "status": "completed",
    })
    text = result[0].text
    # Per-task error wrapping varies; either a top-level Unauthorized
    # or a per-task error message is acceptable. Just assert the
    # update did NOT take effect.
    listing = client.get("/api/tasks").json()
    if isinstance(listing, dict):
        listing = listing.get("tasks", [])
    row = next((r for r in listing if r["task_id"] == task_id), None)
    assert row is not None
    assert row["status"] != "completed", (
        f"bob (not assigned) successfully completed alice's task — "
        "permission boundary broken: {text}"
    )
