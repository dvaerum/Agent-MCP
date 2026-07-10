"""AZ-R26-1 — capability-routing parity across every reassign path.

The Mode-3 self-claim assign path (``_assign_to_existing_tasks``)
enforces the routing control ``required_capabilities ⊆ agent.capabilities``
(round-1 fix): a caller cannot pin a capability-tagged task onto an
agent that lacks the capability. Every OTHER reassign/create path skipped
that check while still enforcing terminal-sink + assignability:

  * MCP ``bulk_task_operations`` ``reassign`` op
  * MCP ``update_task_status`` single reassign (``_update_single_task``)
  * REST ``POST /api/update-task-dashboard`` reassign (composition)
  * REST ``POST /api/tasks`` create-with-``assigned_to``

So a manager/operator carrying ``tasks.assign`` could route a task
tagged ``required_capabilities: ["deploy"]`` onto an agent lacking
``deploy`` via any of those paths — the canonical assign path refuses
exactly this. This suite pins the missing gate on all four paths.

Tests assert against the DB directly (authoritative source).
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_agent_with_caps(agent_id: str, caps: list[str]) -> None:
    """Insert a live agent row carrying ``caps`` + mirror into cache."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    token = secrets.token_hex(16)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at, agent_role) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token, agent_id, json.dumps(caps), now, "active",
            "/tmp", "#888", now, "worker",
        ),
    )
    conn.commit()
    conn.close()
    g.active_agents[token] = {
        "agent_id": agent_id,
        "status": "active",
        "created_at": now,
        "capabilities": caps,
        "agent_role": "worker",
    }


def _seed_task(
    title: str, assigned_to: str | None, *, status: str = "pending",
    required_capabilities: list[str] | None = None,
) -> str:
    """Insert a task row (+ cache) with an optional required-caps tag."""
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    task_id = f"task_{secrets.token_hex(6)}"
    now = _dt.datetime.now().isoformat()
    req = json.dumps(required_capabilities) if required_capabilities else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (task_id, title, description, status, priority, "
        "assigned_to, created_by, created_at, updated_at, notes, "
        "required_capabilities) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title, "seed", status, "low",
            assigned_to, "admin", now, now, "[]", req,
        ),
    )
    conn.commit()
    conn.close()
    g.tasks[task_id] = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "priority": "low",
        "assigned_to": assigned_to,
        "created_by": "admin",
        "created_at": now,
        "updated_at": now,
        "notes": [],
        "required_capabilities": required_capabilities or [],
    }
    return task_id


def _db_task(task_id: str) -> dict:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status, assigned_to FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"task {task_id} vanished"
    return dict(row)


# ==========================================================================
# RED — MCP bulk reassign must refuse an under-capable target
# ==========================================================================


async def test_bulk_reassign_undercapable_agent_denied(tmp_path) -> None:
    """Bulk ``reassign`` of a ``required_capabilities: ["deploy"]`` task
    onto an agent lacking ``deploy`` must be DENIED — matching the assign
    path. RED on origin/main (bulk writes ``assigned_to`` unconditionally)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("bob", [])  # lacks deploy
        task_id = _seed_task(
            "deploy-tagged", "alice", required_capabilities=["deploy"]
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id, "assigned_to": "bob"},
            ]},
        )
        text = result[0].text

        assert "reassigned to 'bob'" not in text, text
        assert "capabilit" in text.lower(), text
        assert _db_task(task_id)["assigned_to"] == "alice", text


async def test_bulk_reassign_capable_agent_succeeds(tmp_path) -> None:
    """Regression: reassigning the same tagged task onto an agent that
    DOES carry ``deploy`` still succeeds."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("carol", ["deploy"])
        task_id = _seed_task(
            "deploy-tagged", "alice", required_capabilities=["deploy"]
        )

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id,
                 "assigned_to": "carol"},
            ]},
        )
        text = result[0].text
        assert "reassigned to 'carol'" in text, text
        assert _db_task(task_id)["assigned_to"] == "carol", text


async def test_bulk_reassign_untagged_task_any_agent_succeeds(tmp_path) -> None:
    """Regression: a task with NO required_capabilities can be reassigned
    to any live agent (empty required set always satisfies)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("bob", [])
        task_id = _seed_task("untagged", "alice")

        result = await admin.call(
            "bulk_task_operations",
            {"operations": [
                {"type": "reassign", "task_id": task_id, "assigned_to": "bob"},
            ]},
        )
        text = result[0].text
        assert "reassigned to 'bob'" in text, text
        assert _db_task(task_id)["assigned_to"] == "bob", text


# ==========================================================================
# RED — MCP single update_task reassign must refuse an under-capable target
# ==========================================================================


async def test_single_update_task_reassign_undercapable_denied(
    tmp_path,
) -> None:
    """``update_task_status`` with an ``assigned_to`` that lacks the
    task's required capability must be refused. RED on origin/main
    (``_update_single_task`` admin reassign only checks assignability)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("bob", [])
        task_id = _seed_task(
            "deploy-tagged", "alice", required_capabilities=["deploy"]
        )

        result = await admin.call(
            "update_task_status",
            {"task_id": task_id, "status": "in_progress",
             "assigned_to": "bob"},
        )
        text = result[0].text
        assert "capabilit" in text.lower(), text
        row = _db_task(task_id)
        assert row["assigned_to"] == "alice", text
        assert row["status"] == "pending", text


# ==========================================================================
# RED — REST dashboard composition reassign must refuse an under-capable
# target
# ==========================================================================


async def test_composition_reassign_undercapable_denied(tmp_path) -> None:
    """``POST /api/update-task-dashboard`` reassign onto an under-capable
    agent must 4xx. RED on origin/main (composition writes assigned_to)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("bob", [])
        task_id = _seed_task(
            "deploy-tagged", "alice", required_capabilities=["deploy"]
        )

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id,
                  "assigned_to": "bob"},
        )
        assert r.status_code != 200, r.text
        assert _db_task(task_id)["assigned_to"] == "alice", r.text


async def test_composition_reassign_capable_agent_succeeds(tmp_path) -> None:
    """Regression: composition reassign to a capable agent still 200s."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("carol", ["deploy"])
        task_id = _seed_task(
            "deploy-tagged", "alice", required_capabilities=["deploy"]
        )

        r = admin.client.post(
            "/api/update-task-dashboard",
            json={"token": admin.admin_token, "task_id": task_id,
                  "assigned_to": "carol"},
        )
        assert r.status_code == 200, r.text
        assert _db_task(task_id)["assigned_to"] == "carol", r.text


# ==========================================================================
# RED — REST create-with-assigned_to must refuse an under-capable target
# ==========================================================================


async def test_rest_create_undercapable_assign_denied(tmp_path) -> None:
    """``POST /api/tasks`` creating a ``required_capabilities`` task
    directly assigned to an under-capable agent must 4xx. RED on
    origin/main (create writes assigned_to after only assignability)."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent_with_caps("bob", [])

        r = admin.client.post(
            "/api/tasks",
            json={
                "token": admin.admin_token,
                "task_title": "deploy-tagged",
                "assigned_to": "bob",
                "required_capabilities": ["deploy"],
            },
        )
        assert r.status_code != 200, r.text


# ==========================================================================
# RED — MCP assign_task Mode-1 create+assign must refuse an under-capable
# target (5th sibling found by the class-sweep)
# ==========================================================================


async def test_assign_task_mode1_undercapable_assign_denied(tmp_path) -> None:
    """``assign_task`` Mode-1 (agent_token + title/description) creating a
    ``required_capabilities``-tagged task and pinning it on an
    under-capable agent must be refused — the SAME create-time control the
    REST create path enforces. RED on origin/main (Mode-1 create checks
    only ``_agent_assignable``)."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")  # capabilities == []

        result = await admin.call(
            "assign_task",
            {
                "agent_token": bob.token,
                "task_title": "deploy-tagged",
                "task_description": "ship it",
                "required_capabilities": ["deploy"],
            },
        )
        text = result[0].text
        assert "capabilit" in text.lower(), text
        # No task should have landed on bob.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE assigned_to = ?",
                ("bob",),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0, f"an under-capable task was pinned on bob: {text}"


async def test_assign_task_mode1_capable_assign_succeeds(tmp_path) -> None:
    """Regression: Mode-1 create+assign to a capable agent still works."""
    async with mcp_session(tmp_path) as admin:
        carol = await admin.create_worker("carol")
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE agents SET capabilities = ? WHERE agent_id = ?",
                (json.dumps(["deploy"]), "carol"),
            )
            conn.commit()
        finally:
            conn.close()

        result = await admin.call(
            "assign_task",
            {
                "agent_token": carol.token,
                "task_title": "deploy-tagged",
                "task_description": "ship it",
                "required_capabilities": ["deploy"],
            },
        )
        text = result[0].text
        assert "capabilit" not in text.lower(), text
        assert "Error" not in text, text
