"""Restore + Purge actions for terminated agents (PR: agent-restore-and-purge-cascade).

`terminate_agent` is a soft-delete: it flips `status='terminated'` on the
row but keeps every reference intact. The Agents-page UI renders that
state but offers no follow-up; admins want a two-step destructive flow:
Terminate (existing) → Restore (reverse soft-delete) OR Purge (hard
delete + cascade tombstone rewrite).

These tests cover the backend half of the PR:
- POST   /api/agents/<id>/restore        — admin reverses soft-delete
- GET    /api/agents/<id>/purge-preview  — blast-radius counts + samples
- DELETE /api/agents/<id>?cascade=true   — hard delete + tombstone cascade
- Agent-ID validation: reject `[` / `]` so tombstone literal stays unambiguous
- Worker tokens get 403 on all three endpoints (admin-only)

Cascade contract (locked with Dennis):
  agents          → DELETE row (last in tx)
  agent_messages  → tombstone sender_id, recipient_id → '[deleted-<id>]'
  tasks           → tombstone created_by; SET NULL assigned_to + status='unassigned'
  agent_actions   → tombstone agent_id
  tasks.notes JSON → UNTOUCHED (preserved as audit trail)
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import secrets

import pytest


# ---- fixtures borrowed from test_assign_task_agent_token.py style -----

def _admin(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _seed_worker(name: str = "alice", status: str = "active") -> tuple[str, str]:
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
        (worker_token, name, "[]", now, status, "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()

    if status != "terminated":
        g.active_agents[worker_token] = {
            "agent_id": name,
            "status": status,
            "created_at": now,
            "capabilities": [],
        }
    return worker_token, name


def _terminate(client, agent_id: str) -> None:
    """Use the existing terminate_agent admin tool to do a real soft-delete."""
    from agent_mcp.tools.admin_tools import terminate_agent_tool_impl

    admin = _admin(client)
    result = asyncio.run(
        terminate_agent_tool_impl({"token": admin, "agent_id": agent_id})
    )
    text = result[0].text
    assert "terminated" in text.lower(), f"terminate failed: {text}"


def _row(client, table: str, where_sql: str, params: tuple) -> dict | None:
    """Read a single row directly from the test DB."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _count(client, table: str, where_sql: str = "1=1",
           params: tuple = ()) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where_sql}",
                       params)
        return cursor.fetchone()["n"]
    finally:
        conn.close()


def _insert_message(sender: str, recipient: str, content: str,
                    ts: str | None = None) -> str:
    from agent_mcp.db.connection import get_db_connection

    msg_id = f"msg_{secrets.token_hex(6)}"
    ts = ts or _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
            "message_content, message_type, priority, timestamp, delivered, read) "
            "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
            (msg_id, sender, recipient, content, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def _insert_task(task_id: str, title: str, created_by: str,
                 assigned_to: str | None, notes_author: str | None = None) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    notes_json = "[]"
    if notes_author:
        notes_json = json.dumps([
            {"timestamp": now, "author": notes_author, "content": "first note"}
        ])
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', ?, ?, ?, 'medium', ?, ?, NULL, '[]', '[]', ?)",
            (task_id, title, assigned_to, created_by,
             "pending" if assigned_to else "unassigned",
             now, now, notes_json),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_action(agent_id: str, action_type: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_actions (agent_id, action_type, task_id, "
            "timestamp, details) VALUES (?, ?, NULL, ?, '{}')",
            (agent_id, action_type, _dt.datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------- restore tests --------------------------------

def test_restore_flips_status_back_to_created(client) -> None:
    """POST /api/agents/<id>/restore reverses status='terminated' →
    status='created' and clears terminated_at. The agent reappears in
    g.active_agents (so the dashboard's token list and active filter
    pick it up)."""
    from agent_mcp.core import globals as g

    _seed_worker("alice")
    _terminate(client, "alice")

    row = _row(client, "agents", "agent_id = ?", ("alice",))
    assert row is not None and row["status"] == "terminated"
    assert row["terminated_at"] is not None

    admin = _admin(client)
    resp = client.post(
        "/api/agents/alice/restore",
        json={"token": admin},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("success") is True, body

    row2 = _row(client, "agents", "agent_id = ?", ("alice",))
    assert row2 is not None
    assert row2["status"] == "created", row2
    assert row2["terminated_at"] is None, row2

    # Restored agent must be back in the in-memory active map.
    matching_tokens = [
        tok for tok, data in g.active_agents.items()
        if data.get("agent_id") == "alice"
    ]
    assert matching_tokens, (
        "restored agent must re-appear in g.active_agents so the "
        "dashboard/list endpoints see it as active"
    )


def test_restore_logs_audit_action(client) -> None:
    """Restoring writes a `restored_agent` row to agent_actions."""
    _seed_worker("alice")
    _terminate(client, "alice")
    admin = _admin(client)

    resp = client.post("/api/agents/alice/restore", json={"token": admin})
    assert resp.status_code == 200

    n = _count(client, "agent_actions",
               "action_type = ? AND agent_id = ?",
               ("restored_agent", "admin"))
    assert n >= 1, "restore must log an agent_actions row"


def test_restore_rejects_worker_token(client) -> None:
    """Worker tokens must not be able to restore agents."""
    _seed_worker("alice")
    _terminate(client, "alice")
    worker_token, _ = _seed_worker("bob")

    resp = client.post("/api/agents/alice/restore",
                       json={"token": worker_token})
    assert resp.status_code in (401, 403), resp.text


def test_restore_404_when_agent_missing(client) -> None:
    admin = _admin(client)
    resp = client.post("/api/agents/nonexistent/restore",
                       json={"token": admin})
    assert resp.status_code == 404, resp.text


def test_restore_rejects_active_agent(client) -> None:
    """Restoring a non-terminated agent is a no-op; return 409."""
    _seed_worker("alice")  # status='active'
    admin = _admin(client)
    resp = client.post("/api/agents/alice/restore", json={"token": admin})
    assert resp.status_code in (400, 409), resp.text


# ----------------------- purge-preview tests --------------------------

def test_purge_preview_returns_counts(client) -> None:
    """GET /api/agents/<id>/purge-preview returns counts that match what
    the cascade would tombstone."""
    _seed_worker("alice")
    _terminate(client, "alice")

    # Seed cascade data.
    _insert_message("alice", "bob", "hello world from alice")
    _insert_message("alice", "bob", "second message")
    _insert_message("bob", "alice", "reply to alice")
    _insert_task("task_aaa", "fix the X", created_by="alice",
                 assigned_to=None)
    _insert_task("task_bbb", "do the Y", created_by="bob",
                 assigned_to="alice")
    _insert_action("alice", "created_agent")
    _insert_action("alice", "claimed_task")

    admin = _admin(client)
    resp = client.get(
        f"/api/agents/alice/purge-preview?token={admin}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "alice"
    assert body["counts"]["messages_sent"] == 2
    assert body["counts"]["messages_received"] == 1
    assert body["counts"]["tasks_created"] == 1
    assert body["counts"]["tasks_assigned"] == 1
    assert body["counts"]["agent_actions"] >= 2  # plus terminate's own log
    # samples optional but should be present and non-empty for non-zero counts
    assert "samples" in body


def test_purge_preview_rejects_worker_token(client) -> None:
    _seed_worker("alice")
    _terminate(client, "alice")
    worker_token, _ = _seed_worker("bob")

    resp = client.get(
        f"/api/agents/alice/purge-preview?token={worker_token}"
    )
    assert resp.status_code in (401, 403), resp.text


# ----------------------- purge cascade tests --------------------------

def test_purge_cascade_full(client) -> None:
    """DELETE /api/agents/<id>?cascade=true performs the full cascade:
    - DELETE the agents row
    - tombstone sender_id/recipient_id in agent_messages → '[deleted-<id>]'
    - tombstone created_by in tasks
    - SET NULL assigned_to + status='unassigned' in tasks
    - tombstone agent_id in agent_actions
    - LEAVE tasks.notes JSON untouched
    """
    _seed_worker("alice")
    _terminate(client, "alice")

    # Messages: alice sent some + received some.
    msg_sent = _insert_message("alice", "bob", "from alice 1")
    msg_recv = _insert_message("bob", "alice", "to alice 1")

    # Tasks: alice created task1, alice is assigned task2 (+ alice's
    # notes get preserved on task2).
    _insert_task("task_created_by_alice", "by alice", created_by="alice",
                 assigned_to=None, notes_author="alice")
    _insert_task("task_assigned_to_alice", "for alice", created_by="bob",
                 assigned_to="alice", notes_author="alice")

    # Actions.
    _insert_action("alice", "claimed_task")

    admin = _admin(client)
    resp = client.request(
        "DELETE",
        "/api/agents/alice",
        params={"cascade": "true"},
        json={"token": admin},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("success") is True
    # Counts of what was tombstoned should be reported.
    assert body["counts"]["messages_sent"] == 1
    assert body["counts"]["messages_received"] == 1
    assert body["counts"]["tasks_created"] == 1
    assert body["counts"]["tasks_assigned"] == 1

    tombstone = "[deleted-alice]"

    # agents row gone.
    assert _row(client, "agents", "agent_id = ?", ("alice",)) is None

    # messages tombstoned.
    m1 = _row(client, "agent_messages", "message_id = ?", (msg_sent,))
    m2 = _row(client, "agent_messages", "message_id = ?", (msg_recv,))
    assert m1 is not None and m1["sender_id"] == tombstone
    assert m2 is not None and m2["recipient_id"] == tombstone

    # tasks: created_by tombstoned on first; assigned_to NULL + status
    # unassigned on second.
    t1 = _row(client, "tasks", "task_id = ?", ("task_created_by_alice",))
    assert t1 is not None
    assert t1["created_by"] == tombstone
    t2 = _row(client, "tasks", "task_id = ?", ("task_assigned_to_alice",))
    assert t2 is not None
    assert t2["assigned_to"] is None, t2
    assert t2["status"] == "unassigned", t2

    # notes JSON: untouched — must still mention 'alice' as the author.
    notes_t1 = json.loads(t1["notes"] or "[]")
    notes_t2 = json.loads(t2["notes"] or "[]")
    assert any(n.get("author") == "alice" for n in notes_t1), (
        "notes JSON must be preserved as an audit trail; original "
        f"author 'alice' should still be present in notes_t1 = {notes_t1}"
    )
    assert any(n.get("author") == "alice" for n in notes_t2), (
        "notes JSON must be preserved as an audit trail; original "
        f"author 'alice' should still be present in notes_t2 = {notes_t2}"
    )

    # agent_actions tombstoned (every row that was agent_id='alice').
    remaining_with_alice = _count(client, "agent_actions",
                                  "agent_id = ?", ("alice",))
    assert remaining_with_alice == 0, (
        f"agent_actions should have no rows referencing raw 'alice'; "
        f"{remaining_with_alice} remain"
    )
    tombstoned_actions = _count(client, "agent_actions",
                                "agent_id = ?", (tombstone,))
    assert tombstoned_actions >= 1


def test_purge_atomic_on_failure(client) -> None:
    """If the cascade hits an error mid-flight, NOTHING gets written.
    Verified by attempting to purge a non-existent agent — the
    transaction starts, hits the missing-agent check (or fails on the
    DELETE), and rolls back. No tombstones for unrelated rows."""
    # Bootstrap a different agent + its message that must not be touched.
    _seed_worker("alice")
    msg = _insert_message("alice", "bob", "untouched")

    admin = _admin(client)
    resp = client.request(
        "DELETE",
        "/api/agents/ghost",
        params={"cascade": "true"},
        json={"token": admin},
    )
    assert resp.status_code == 404, resp.text

    # Unrelated message must remain as-is.
    m = _row(client, "agent_messages", "message_id = ?", (msg,))
    assert m is not None
    assert m["sender_id"] == "alice"
    assert m["recipient_id"] == "bob"


def test_purge_requires_cascade_flag(client) -> None:
    """DELETE /api/agents/<id> without cascade=true must refuse —
    we don't accidentally hard-delete via a bare DELETE."""
    _seed_worker("alice")
    _terminate(client, "alice")
    admin = _admin(client)

    resp = client.request(
        "DELETE",
        "/api/agents/alice",
        json={"token": admin},
    )
    assert resp.status_code == 400, resp.text


def test_purge_rejects_worker_token(client) -> None:
    _seed_worker("alice")
    _terminate(client, "alice")
    worker_token, _ = _seed_worker("bob")
    resp = client.request(
        "DELETE",
        "/api/agents/alice",
        params={"cascade": "true"},
        json={"token": worker_token},
    )
    assert resp.status_code in (401, 403), resp.text


def test_purge_404_for_missing_agent(client) -> None:
    admin = _admin(client)
    resp = client.request(
        "DELETE",
        "/api/agents/nonexistent",
        params={"cascade": "true"},
        json={"token": admin},
    )
    assert resp.status_code == 404, resp.text


# -------------- agent_id validation: forbid [ and ] -------------------

@pytest.mark.parametrize("bad_id", [
    "[bad]",
    "alice[",
    "alice]",
    "[deleted-alice]",
    "team[1]worker",
])
def test_create_agent_rejects_brackets(client, bad_id: str) -> None:
    """create_agent must reject any agent_id containing [ or ] so the
    cascade tombstone literal `[deleted-<id>]` stays unambiguous."""
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    admin = _admin(client)
    # create a placeholder task we can claim — required by create_agent.
    _insert_task("task_placeholder", "ph", created_by="admin",
                 assigned_to=None)

    result = asyncio.run(create_agent_tool_impl({
        "token": admin,
        "agent_id": bad_id,
        "task_ids": ["task_placeholder"],
    }))
    text = result[0].text.lower()
    assert "error" in text or "invalid" in text, (
        f"create_agent should reject agent_id '{bad_id}' with [ or ]; got: {text!r}"
    )


def test_create_agent_dashboard_api_rejects_brackets(client) -> None:
    """The dashboard REST shim also rejects brackets, before delegating
    to the admin tool (defense in depth, and clearer error code)."""
    admin = _admin(client)
    resp = client.post("/api/create-agent", json={
        "token": admin,
        "agent_id": "[bad]",
    })
    # Should be 400 (validation), not 500 (tool-level rejection).
    assert resp.status_code == 400, resp.text
