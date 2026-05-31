"""assign_task honors agent_token on create (issue L).

Per UPSTREAM_ISSUES.md issue L: calling
`assign_task(token=admin, agent_token=worker, task_title=X,
task_description=Y)` should create AND assign in one call. The
documented bug was that agent_token got ignored — task created
but assigned_to was empty.

The fix may already be in upstream (Mode 1 of assign_task sets
assigned_to = target_agent_id at line 1502). This test verifies it.

Also tests:
- issue M: assign_task moves a previously-unassigned task to
  status='pending' when it acquires assigned_to.
- The router's `create_task_for_self` and `claim_task` synthetics
  can retire once these work natively.
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


def _call_assign(arguments: dict):
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    return asyncio.run(assign_task_tool_impl(arguments))


def _row(client, task_id: str):
    """Lookup task row via REST. /api/tasks returns a plain list of rows."""
    listing = client.get("/api/tasks").json()
    if isinstance(listing, dict):
        listing = listing.get("tasks", [])
    for entry in listing:
        if entry.get("task_id") == task_id:
            return entry
    return None


def test_assign_task_create_and_assign_in_one_call(client) -> None:
    """Mode 1: agent_token + task_title/description → task created
    AND assigned to the agent in one call (issue L)."""
    admin = _admin(client)
    worker_token, worker_id = _seed_worker("alice")

    result = _call_assign({
        "token": admin,
        "agent_token": worker_token,
        "task_title": "do the thing",
        "task_description": "all of it",
    })
    text = result[0].text
    assert "Error" not in text and "error" not in text, text

    # The result text should mention the task_id.
    import re
    m = re.search(r"task_[a-f0-9]+", text)
    assert m, f"no task_id in result: {text}"
    task_id = m.group(0)

    # Look up the row — assigned_to must be the worker's agent_id.
    row = _row(client, task_id)
    assert row is not None, f"task {task_id} not in /api/tasks listing"
    assert row.get("assigned_to") == worker_id, (
        f"expected assigned_to=={worker_id}, got {row.get('assigned_to')!r}; "
        "issue L would manifest as empty assigned_to"
    )


def test_assign_task_status_pending_when_assigned(client) -> None:
    """Issue M: a newly assigned task must have status 'pending', not
    'unassigned'. Otherwise the dashboard's 'Unassigned' filter shows
    the task even after assignment."""
    admin = _admin(client)
    worker_token, _ = _seed_worker("alice")

    result = _call_assign({
        "token": admin,
        "agent_token": worker_token,
        "task_title": "do the thing",
        "task_description": "all of it",
    })
    import re
    task_id = re.search(r"task_[a-f0-9]+", result[0].text).group(0)
    row = _row(client, task_id)
    assert row is not None
    assert row.get("status") in ("pending", "in_progress"), (
        f"newly-assigned task has status {row.get('status')!r}; "
        "issue M would manifest as 'unassigned'"
    )
