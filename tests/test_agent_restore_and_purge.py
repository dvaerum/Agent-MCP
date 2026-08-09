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

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session, seed_agent_rows

pytestmark = pytest.mark.asyncio


# ---- helpers ---------------------------------------------------------


async def _terminate(admin, agent_id: str) -> None:
    """Use the existing terminate_agent admin tool to do a real
    soft-delete."""
    result = await admin.call(
        "terminate_agent", {"agent_id": agent_id}
    )
    text = result[0].text
    assert "terminated" in text.lower(), f"terminate failed: {text}"


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
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


def _count(table: str, where_sql: str = "1=1", params: tuple = ()) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where_sql}", params,
        )
        return cursor.fetchone()["n"]
    finally:
        conn.close()


def _insert_message(
    sender: str, recipient: str, content: str, ts: str | None = None,
) -> str:
    from agent_mcp.db.connection import get_db_connection

    msg_id = f"msg_{secrets.token_hex(6)}"
    ts = ts or _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
            "message_content, message_type, priority, timestamp, "
            "delivered, read) "
            "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
            (msg_id, sender, recipient, content, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def _insert_task(
    task_id: str, title: str, created_by: str,
    assigned_to: str | None, notes_author: str | None = None,
) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    notes_json = "[]"
    if notes_author:
        notes_json = json.dumps([
            {"timestamp": now, "author": notes_author, "content": "first note"}
        ])
    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Restore/purge acts on assigned_to/created_by, not
    # parentage, and no extra row is added.
    parent = existing_root_task_id()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', ?, ?, ?, 'medium', ?, ?, ?, '[]', '[]', ?)",
            (task_id, title, assigned_to, created_by,
             "pending" if assigned_to else "unassigned",
             now, now, parent, notes_json),
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


async def test_restore_flips_status_back_to_created(tmp_path) -> None:
    """POST /api/agents/<id>/restore reverses status='terminated' →
    status='created' and clears terminated_at. The agent reappears in
    g.active_agents (so the dashboard's token list and active filter
    pick it up)."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None and row["status"] == "terminated"
        assert row["terminated_at"] is not None

        resp = admin.post(
            "/api/agents/alice/restore",
            json={},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("success") is True, body

        row2 = _row("agents", "agent_id = ?", ("alice",))
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


async def test_restore_logs_audit_action(tmp_path) -> None:
    """Restoring writes a `restored_agent` row to agent_actions."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        # Operator-tier Bearer (not the forwarding header) so the audit
        # row is attributed to "admin" — caller_identity() falls back to
        # "admin" on the operator_bearer path, whereas the forwarding
        # header would attribute the action to the operator id.
        resp = admin.client.post(
            "/api/agents/alice/restore",
            json={},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert resp.status_code == 200

        n = _count(
            "agent_actions",
            "action_type = ? AND agent_id = ?",
            ("restored_agent", "admin"),
        )
        assert n >= 1, "restore must log an agent_actions row"


async def test_restore_rejects_worker_token(tmp_path) -> None:
    """Worker tokens must not be able to restore agents."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")
        bob = await admin.create_worker("bob")

        # Worker bearer: exercises the operator-tier gate (non-operator
        # bearer rejected), not merely the no-auth 401 path.
        resp = admin.client.post(
            "/api/agents/alice/restore",
            json={},
            headers={"Authorization": f"Bearer {bob.token}"},
        )
        assert resp.status_code in (401, 403), resp.text


async def test_restore_404_when_agent_missing(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/agents/nonexistent/restore",
            json={},
        )
        assert resp.status_code == 404, resp.text


async def test_restore_rejects_active_agent(tmp_path) -> None:
    """Restoring a non-terminated agent is a no-op; return 409."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")  # status='active'
        resp = admin.post(
            "/api/agents/alice/restore", json={},
        )
        assert resp.status_code in (400, 409), resp.text


# ----------------------- purge-preview tests --------------------------


async def test_purge_preview_returns_counts(tmp_path) -> None:
    """GET /api/agents/<id>/purge-preview returns counts that match what
    the cascade would tombstone."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # bob is referenced via raw _insert_message but never created
        # as a real worker; seed the agents row so the
        # agent_messages.{sender_id, recipient_id} FK (PR-G1) accepts
        # the insert.
        seed_agent_rows("bob")
        await _terminate(admin, "alice")

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

        resp = admin.get(
            "/api/agents/alice/purge-preview"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == "alice"
        assert body["counts"]["messages_sent"] == 2
        assert body["counts"]["messages_received"] == 1
        assert body["counts"]["tasks_created"] == 1
        assert body["counts"]["tasks_assigned"] == 1
        assert body["counts"]["agent_actions"] >= 2  # plus terminate's log
        assert "samples" in body


async def test_purge_preview_rejects_worker_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")
        bob = await admin.create_worker("bob")

        # Worker bearer: exercises the operator-tier gate, not no-auth 401.
        resp = admin.client.get(
            "/api/agents/alice/purge-preview",
            headers={"Authorization": f"Bearer {bob.token}"},
        )
        assert resp.status_code in (401, 403), resp.text


# ----------------------- purge cascade tests --------------------------


async def test_purge_cascade_full(tmp_path) -> None:
    """DELETE /api/agents/<id>?cascade=true performs the full cascade:
    - DELETE the agents row
    - tombstone sender_id/recipient_id in agent_messages → '[deleted-<id>]'
    - tombstone created_by in tasks
    - SET NULL assigned_to + status='unassigned' in tasks
    - tombstone agent_id in agent_actions
    - LEAVE tasks.notes JSON untouched
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        seed_agent_rows("bob")  # PR-G1 FK requires the row
        await _terminate(admin, "alice")

        # Messages: alice sent some + received some.
        msg_sent = _insert_message("alice", "bob", "from alice 1")
        msg_recv = _insert_message("bob", "alice", "to alice 1")

        # Tasks: alice created task1, alice is assigned task2 (+ alice's
        # notes get preserved on task2).
        _insert_task("task_created_by_alice", "by alice",
                     created_by="alice", assigned_to=None,
                     notes_author="alice")
        _insert_task("task_assigned_to_alice", "for alice",
                     created_by="bob", assigned_to="alice",
                     notes_author="alice")

        # Actions.
        _insert_action("alice", "claimed_task")

        resp = admin.request(
            "DELETE",
            "/api/agents/alice",
            params={"cascade": "true"},
            json={},
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
        assert _row("agents", "agent_id = ?", ("alice",)) is None

        # messages tombstoned.
        m1 = _row("agent_messages", "message_id = ?", (msg_sent,))
        m2 = _row("agent_messages", "message_id = ?", (msg_recv,))
        assert m1 is not None and m1["sender_id"] == tombstone
        assert m2 is not None and m2["recipient_id"] == tombstone

        # tasks: created_by tombstoned on first; assigned_to NULL +
        # status unassigned on second.
        t1 = _row("tasks", "task_id = ?", ("task_created_by_alice",))
        assert t1 is not None
        assert t1["created_by"] == tombstone
        t2 = _row("tasks", "task_id = ?", ("task_assigned_to_alice",))
        assert t2 is not None
        assert t2["assigned_to"] is None, t2
        assert t2["status"] == "unassigned", t2

        # notes JSON: untouched — must still mention 'alice' as author.
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
        remaining_with_alice = _count(
            "agent_actions", "agent_id = ?", ("alice",),
        )
        assert remaining_with_alice == 0, (
            f"agent_actions should have no rows referencing raw 'alice'; "
            f"{remaining_with_alice} remain"
        )
        tombstoned_actions = _count(
            "agent_actions", "agent_id = ?", (tombstone,),
        )
        assert tombstoned_actions >= 1


async def test_purge_atomic_on_failure(tmp_path) -> None:
    """If the cascade hits an error mid-flight, NOTHING gets written.
    Verified by attempting to purge a non-existent agent — the
    transaction starts, hits the missing-agent check (or fails on the
    DELETE), and rolls back. No tombstones for unrelated rows."""
    async with mcp_session(tmp_path) as admin:
        # Bootstrap a different agent + its message that must not be touched.
        await admin.create_worker("alice")
        seed_agent_rows("bob")  # PR-G1 FK requires the row
        msg = _insert_message("alice", "bob", "untouched")

        resp = admin.request(
            "DELETE",
            "/api/agents/ghost",
            params={"cascade": "true"},
            json={},
        )
        assert resp.status_code == 404, resp.text

        # Unrelated message must remain as-is.
        m = _row("agent_messages", "message_id = ?", (msg,))
        assert m is not None
        assert m["sender_id"] == "alice"
        assert m["recipient_id"] == "bob"


async def test_purge_requires_cascade_flag(tmp_path) -> None:
    """DELETE /api/agents/<id> without cascade=true must refuse —
    we don't accidentally hard-delete via a bare DELETE."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        resp = admin.request(
            "DELETE",
            "/api/agents/alice",
            json={},
        )
        assert resp.status_code == 400, resp.text


async def test_purge_rejects_worker_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")
        bob = await admin.create_worker("bob")
        # Worker bearer: exercises the operator-tier gate, not no-auth 401.
        resp = admin.client.request(
            "DELETE",
            "/api/agents/alice",
            params={"cascade": "true"},
            json={},
            headers={"Authorization": f"Bearer {bob.token}"},
        )
        assert resp.status_code in (401, 403), resp.text


async def test_purge_404_for_missing_agent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.request(
            "DELETE",
            "/api/agents/nonexistent",
            params={"cascade": "true"},
            json={},
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
async def test_register_agent_rejects_brackets(tmp_path, bad_id: str) -> None:
    """register_agent must reject any agent_id containing [ or ] so the
    cascade tombstone literal `[deleted-<id>]` stays unambiguous.

    Wave 7 PR 1 (coordinator transition): migrated from the
    ``create_agent`` MCP tool (legacy spawn path that orphan-stormed
    claude processes) to ``register_agent`` (the spawnless sibling
    shipped in Wave 7 PR 0). The bracket guard is preserved verbatim
    in ``register_agent_tool_impl`` — same defence-in-depth wording
    as the legacy impl.
    """
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "register_agent",
            {"agent_id": bad_id},
        )
        text = result[0].text.lower()
        assert "error" in text or "invalid" in text or "reserved" in text, (
            f"register_agent should reject agent_id '{bad_id}' with [ or ]; "
            f"got: {text!r}"
        )


async def test_register_agent_dashboard_api_rejects_brackets(tmp_path) -> None:
    """The dashboard REST shim also rejects brackets, before / via the
    tool impl (defense in depth, and clearer error code).

    Wave 7 PR 1: migrated from POST /api/create-agent (back-compat
    alias of the legacy spawn endpoint) to POST /api/agents/register
    (the register-only sibling shipped in Wave 7 PR 0). The route
    adapter surfaces the tool's :class:`Invalid` as a 400 — same
    status code, same operator experience.
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.post("/api/agents/register", json={
            "agent_id": "[bad]",
        })
        # Should be 400 (validation), not 500 (tool-level rejection).
        assert resp.status_code == 400, resp.text
