"""assign_task accepts agent_id as admin-only alternative to agent_token (Phase 7d).

This retires the `create_task_for` router synthetic. Single MCP call from admin
should be able to target an agent by agent_id (the human-readable name) without
the caller having to look up the agent's token first.

Behavior:
- Admin + agent_id (no agent_token): resolve agent_id → token server-side, proceed.
- Admin + unknown agent_id: clear "Unknown agent_id: '<id>'" error.
- Worker + agent_id: rejected as admin-only (workers must pass their own token).
- Admin + BOTH agent_id and agent_token: agent_token wins, agent_id ignored.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
import secrets

import pytest


@pytest.fixture(autouse=True)
def _inline_write_queue(monkeypatch):
    """The Mode 0 unassigned-task path (which the admin+unknown-agent_id
    fallback exercises today, pre-impl) routes its INSERT through
    `execute_db_write`, a per-loop asyncio queue that deadlocks when
    invoked via `asyncio.run` from a sync test. Run the operation
    inline so the test surfaces the real error (or success) instead of
    hanging.

    Same shim as `test_worker_self_assign_task.py`.
    """

    async def _inline(operation):
        return await operation()

    monkeypatch.setattr(
        "agent_mcp.tools.task_tools.execute_db_write", _inline
    )


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
    listing = client.get("/api/tasks").json()
    if isinstance(listing, dict):
        listing = listing.get("tasks", [])
    for entry in listing:
        if entry.get("task_id") == task_id:
            return entry
    return None


def test_admin_can_use_agent_id_instead_of_agent_token(client) -> None:
    """Admin passes agent_id='alice' → server resolves to alice's token,
    creates and assigns the task in one call."""
    admin = _admin(client)
    _worker_token, worker_id = _seed_worker("alice")

    result = _call_assign({
        "token": admin,
        "agent_id": worker_id,
        "task_title": "do the thing",
        "task_description": "all of it",
    })
    text = result[0].text
    assert "Error" not in text and "error" not in text, text

    m = re.search(r"task_[a-f0-9]+", text)
    assert m, f"no task_id in result: {text}"
    task_id = m.group(0)

    row = _row(client, task_id)
    assert row is not None, f"task {task_id} not in /api/tasks listing"
    assert row.get("assigned_to") == worker_id, (
        f"expected assigned_to=={worker_id}, got {row.get('assigned_to')!r}"
    )


def test_admin_unknown_agent_id_returns_clear_error(client) -> None:
    """Admin passes an agent_id that doesn't exist → clear error message
    naming the bad id."""
    admin = _admin(client)

    result = _call_assign({
        "token": admin,
        "agent_id": "ghost-agent",
        "task_title": "x",
        "task_description": "y",
    })
    text = result[0].text
    assert "Unknown agent_id" in text and "ghost-agent" in text, text


def test_worker_cannot_use_agent_id_admin_only(client) -> None:
    """Workers may not pass agent_id (it's an admin-only parameter)."""
    _admin(client)  # ensure admin token initialized
    worker_token, worker_id = _seed_worker("bob")

    result = _call_assign({
        "token": worker_token,
        "agent_id": worker_id,
        "task_title": "x",
        "task_description": "y",
    })
    text = result[0].text
    assert "Unauthorized" in text and "admin-only" in text, text


def test_admin_both_agent_id_and_agent_token_prefers_token(client) -> None:
    """When both are provided, agent_token wins. agent_id is ignored
    silently (no error). The task ends up assigned to the agent_token's
    owner, not the agent_id's owner."""
    admin = _admin(client)
    alice_token, alice_id = _seed_worker("alice")
    _bob_token, bob_id = _seed_worker("bob")

    result = _call_assign({
        "token": admin,
        "agent_id": bob_id,  # decoy — should be ignored
        "agent_token": alice_token,  # this wins
        "task_title": "do the thing",
        "task_description": "all of it",
    })
    text = result[0].text
    assert "Error" not in text and "error" not in text, text

    task_id = re.search(r"task_[a-f0-9]+", text).group(0)
    row = _row(client, task_id)
    assert row is not None
    assert row.get("assigned_to") == alice_id, (
        f"agent_token must win when both provided; got {row.get('assigned_to')!r}"
    )
