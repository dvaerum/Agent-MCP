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
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import secrets

import pytest


@pytest.fixture(autouse=True)
def _inline_write_queue(monkeypatch):
    """Mode 0 (`_create_unassigned_tasks`) routes its INSERT through
    `execute_db_write`, which puts work on a per-loop asyncio.Queue
    drained by a worker started inside the TestClient's loop. Calling
    `asyncio.run(tool_impl(...))` from a test creates a NEW loop, so
    the worker isn't running there — the future returned by
    `queue.put()` never completes and the test hangs.

    Bypass for tests: replace `execute_db_write` with a direct call so
    the operation runs in the test's loop. Production behavior is
    unchanged.
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


def _set_toggle(value: bool) -> None:
    """Set config_allow_worker_create_unassigned in project_context."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cursor = conn.cursor()
    # project_context schema (key, value, description, updated_by, last_updated)
    cursor.execute(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, description, updated_by, last_updated) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "config_allow_worker_create_unassigned",
            "true" if value else "false",
            "test toggle",
            "test",
            _dt.datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


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


def test_worker_can_create_unassigned_task_with_default_toggle(client) -> None:
    """Default (toggle absent → allow): a worker token may call
    `assign_task` in mode 0 (no `agent_token`) and successfully file
    a task into the unassigned pool."""
    worker_token, _ = _seed_worker("alice")

    result = _call_assign({
        "token": worker_token,
        "task_title": "found a bug",
        "task_description": "needs triage",
    })
    text = result[0].text
    assert "Unauthorized" not in text, (
        f"worker token should be permitted to create unassigned tasks "
        f"by default; got {text!r}"
    )

    import re
    m = re.search(r"task_[a-f0-9]+", text)
    assert m, f"no task_id in result: {text}"
    task_id = m.group(0)

    row = _row(client, task_id)
    assert row is not None, f"task {task_id} not in /api/tasks listing"
    assert row.get("status") == "unassigned", (
        f"expected status='unassigned', got {row.get('status')!r}"
    )
    assert not row.get("assigned_to"), (
        f"expected no assigned_to on unassigned task, got "
        f"{row.get('assigned_to')!r}"
    )


def test_worker_create_unassigned_blocked_when_toggle_off(client) -> None:
    """When the admin explicitly turns the toggle off, worker calls
    must be rejected with a clear error pointing at the policy."""
    _set_toggle(False)
    worker_token, _ = _seed_worker("alice")

    result = _call_assign({
        "token": worker_token,
        "task_title": "found a bug",
        "task_description": "needs triage",
    })
    text = result[0].text
    assert "Unauthorized" in text or "denied" in text.lower() or "not permitted" in text.lower() or "disabled" in text.lower(), (
        f"toggle=off must reject worker; got {text!r}"
    )
    # The error should mention the policy / toggle so admin can find
    # the knob to turn it back on.
    assert "config_allow_worker_create_unassigned" in text or "worker" in text.lower(), (
        f"error should reference the policy / worker context to make "
        f"the fix discoverable; got {text!r}"
    )


def test_admin_can_create_unassigned_regardless_of_toggle(client) -> None:
    """Admin retains existing behavior — toggle does not gate admin."""
    _set_toggle(False)
    admin = client.get("/api/tokens").json()["admin_token"]

    result = _call_assign({
        "token": admin,
        "task_title": "ops thing",
        "task_description": "ops desc",
    })
    text = result[0].text
    assert "Unauthorized" not in text, text
    import re
    assert re.search(r"task_[a-f0-9]+", text), f"no task_id in: {text}"


def test_worker_cannot_assign_task_to_others(client) -> None:
    """Even with the toggle on, worker tokens may NOT use the
    agent_token field to assign work to other agents. That's a
    separate (more dangerous) capability, not part of Q6d."""
    worker_token, _ = _seed_worker("alice")
    _, _other_name = _seed_worker("bob")

    # Have to find bob's token to attempt to assign to bob
    from agent_mcp.db.connection import get_db_connection
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT token FROM agents WHERE agent_id = 'bob'")
    bob_token = cur.fetchone()["token"]
    conn.close()

    result = _call_assign({
        "token": worker_token,
        "agent_token": bob_token,
        "task_title": "assign-to-bob",
        "task_description": "alice trying to assign to bob",
    })
    text = result[0].text
    assert "Unauthorized" in text or "denied" in text.lower(), (
        f"worker must not be able to assign tasks to other agents; "
        f"got {text!r}"
    )
