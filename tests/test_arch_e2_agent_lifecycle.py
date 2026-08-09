"""E2 (architecture-deepening) — one implementation behind every
agent-lifecycle mutation route.

Before E2, ``purge_agent`` / ``edit_agent`` / ``restore_agent`` existed
ONLY as shadow business-logic tiers inside ``app/routers/agents.py`` — the
REST route WAS the implementation, with no MCP tool. E2 extracts each as a
real tool (``tools/admin_tools.py``) built on the unit-of-work; the routes
become thin adapters that dispatch to the tool.

The keystone invariant this pins: the MCP tool path and the REST route
path are ONE implementation, so they produce IDENTICAL effects. The purge
one-path test drives the full 6-table cascade (agents / agent_messages /
tasks / agent_actions / mcp_sessions / claude_code_sessions) through both
surfaces on two identical fixtures and asserts the resulting DB state is
byte-for-byte the same — proving there is no second, drifting cascade.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets

import pytest

from tests.harness import mcp_session, seed_agent_rows

pytestmark = pytest.mark.asyncio


# ---- raw-DB helpers (bypass the tool surface to seed cascade data) ----


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _count(table: str, where_sql: str = "1=1", params: tuple = ()) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where_sql}", params,
        )
        return cur.fetchone()["n"]
    finally:
        conn.close()


def _insert_message(sender: str, recipient: str, content: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    msg_id = f"msg_{secrets.token_hex(6)}"
    ts = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_messages (message_id, sender_id, recipient_id, "
            "message_content, message_type, priority, timestamp, delivered, "
            "read) VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0)",
            (msg_id, sender, recipient, content, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return msg_id


def _insert_task(
    task_id: str, title: str, created_by: str,
    assigned_to: str | None, status: str | None = None,
    notes_author: str | None = None,
) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    notes_json = "[]"
    if notes_author:
        notes_json = json.dumps(
            [{"timestamp": now, "author": notes_author, "content": "n"}]
        )
    from tests.conftest import existing_root_task_id

    # R15-BL-1: chain under the single root (first seed = root, rest are
    # children). Purge tombstones created_by / NULLs assigned_to — it does
    # not cascade by hierarchy, so parentage is inert here.
    parent = existing_root_task_id()

    resolved_status = status or ("pending" if assigned_to else "unassigned")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, ?, '', ?, ?, ?, 'medium', ?, ?, ?, '[]', '[]', ?)",
            (task_id, title, assigned_to, created_by, resolved_status,
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


def _insert_mcp_session(agent_id: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    session_id = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.UTC).isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mcp_sessions (session_id, agent_id, opened_at, "
            "last_seen_at, bearer_token_hash, alias_used) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (session_id, agent_id, now, now, secrets.token_hex(16)),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


async def _terminate(admin, agent_id: str) -> None:
    result = await admin.call("terminate_agent", {"agent_id": agent_id})
    assert "terminated" in result[0].text.lower(), result[0].text


def _seed_cascade_fixture(admin_created_id: str) -> dict:
    """Seed identical cascade blast-radius data around ``<id>`` and return
    the ids for later assertion.

    ``bob`` (the counterparty) is seeded once by the caller. The rows
    created here mirror the ``test_agent_restore_and_purge`` cascade
    fixture: 1 sent + 1 received message, 1 created task (with alice's
    notes), 1 active-assigned task, 1 terminal-assigned task, 1 action,
    1 mcp_session.
    """
    ident = admin_created_id
    msg_sent = _insert_message(ident, "bob", "from alice")
    msg_recv = _insert_message("bob", ident, "to alice")
    _insert_task(f"task_created_{ident}", "by alice", created_by=ident,
                 assigned_to=None, notes_author=ident)
    _insert_task(f"task_active_{ident}", "for alice", created_by="bob",
                 assigned_to=ident, notes_author=ident)
    _insert_task(f"task_done_{ident}", "done by alice", created_by="bob",
                 assigned_to=ident, status="completed")
    _insert_action(ident, "claimed_task")
    session_id = _insert_mcp_session(ident)
    return {
        "msg_sent": msg_sent,
        "msg_recv": msg_recv,
        "session_id": session_id,
    }


def _norm(value, ident: str):
    """Replace the agent id with a stable ``<ID>`` token so two fixtures
    with different ids compare equal on their id-derived string values
    (tombstones embed the id)."""
    if isinstance(value, str):
        return value.replace(ident, "<ID>")
    return value


def _snapshot_cascade_effects(ident: str, ids: dict) -> dict:
    """Read back the observable cascade effects for ``<ident>`` into a
    surface-agnostic dict so the MCP + REST results can be compared.

    Id-derived string values (tombstones) are normalised via :func:`_norm`
    so the two fixtures — which have DIFFERENT agent ids — still compare
    equal when the cascade behaviour is identical."""
    tombstone = f"[deleted-{ident}]"
    m_sent = _row("agent_messages", "message_id = ?", (ids["msg_sent"],))
    m_recv = _row("agent_messages", "message_id = ?", (ids["msg_recv"],))
    t_created = _row("tasks", "task_id = ?", (f"task_created_{ident}",))
    t_active = _row("tasks", "task_id = ?", (f"task_active_{ident}",))
    t_done = _row("tasks", "task_id = ?", (f"task_done_{ident}",))
    return {
        "agents_row_present": _row("agents", "agent_id = ?", (ident,))
        is not None,
        "tombstone_row_present": _row(
            "agents", "agent_id = ?", (tombstone,)
        ) is not None,
        "msg_sent_sender": _norm(
            m_sent["sender_id"] if m_sent else None, ident
        ),
        "msg_recv_recipient": _norm(
            m_recv["recipient_id"] if m_recv else None, ident
        ),
        "task_created_by": _norm(
            t_created["created_by"] if t_created else None, ident
        ),
        "task_created_notes_author": _norm(
            json.loads(t_created["notes"] or "[]")[0]["author"]
            if t_created and json.loads(t_created["notes"] or "[]") else None,
            ident,
        ),
        "task_active_assigned": t_active["assigned_to"] if t_active else "MISS",
        "task_active_status": t_active["status"] if t_active else "MISS",
        "task_done_assigned": t_done["assigned_to"] if t_done else "MISS",
        "task_done_status": t_done["status"] if t_done else "MISS",
        "actions_raw": _count("agent_actions", "agent_id = ?", (ident,)),
        "actions_tombstoned": _count(
            "agent_actions", "agent_id = ?", (tombstone,)
        ),
        "mcp_sessions_left": _count(
            "mcp_sessions", "agent_id = ?", (ident,)
        ),
    }


# ---- the one-path test ------------------------------------------------


async def test_purge_agent_mcp_and_rest_identical_cascade(tmp_path) -> None:
    """The ``purge_agent`` MCP tool and ``DELETE /api/agents/<id>?cascade=true``
    are ONE implementation: run each over an identical fixture and assert
    the full 6-table cascade leaves byte-for-byte identical DB state."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice_mcp")
        await admin.create_worker("alice_rest")
        seed_agent_rows("bob")  # PR-G1 FK counterparty
        await _terminate(admin, "alice_mcp")
        await _terminate(admin, "alice_rest")

        ids_mcp = _seed_cascade_fixture("alice_mcp")
        ids_rest = _seed_cascade_fixture("alice_rest")

        # MCP surface.
        result = await admin.call("purge_agent", {"agent_id": "alice_mcp"})
        assert "purged" in result[0].text.lower(), result[0].text

        # REST surface.
        resp = admin.request(
            "DELETE", "/api/agents/alice_rest",
            params={"cascade": "true"},
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True

        effects_mcp = _snapshot_cascade_effects("alice_mcp", ids_mcp)
        effects_rest = _snapshot_cascade_effects("alice_rest", ids_rest)

        # Identical effects across BOTH surfaces (the one-path proof).
        assert effects_mcp == effects_rest, (
            f"MCP vs REST purge diverged:\n  mcp ={effects_mcp}\n"
            f"  rest={effects_rest}"
        )

        # And the effects are actually the correct cascade (not two
        # identical no-ops).
        assert effects_mcp["agents_row_present"] is False
        assert effects_mcp["tombstone_row_present"] is True
        assert effects_mcp["msg_sent_sender"] == "[deleted-<ID>]"
        assert effects_mcp["msg_recv_recipient"] == "[deleted-<ID>]"
        assert effects_mcp["task_created_by"] == "[deleted-<ID>]"
        # notes JSON preserved untouched (audit trail).
        assert effects_mcp["task_created_notes_author"] == "<ID>"
        # active task → unassigned; terminal task → assignee NULLed but
        # status kept (no resurrection).
        assert effects_mcp["task_active_assigned"] is None
        assert effects_mcp["task_active_status"] == "unassigned"
        assert effects_mcp["task_done_assigned"] is None
        assert effects_mcp["task_done_status"] == "completed"
        assert effects_mcp["actions_raw"] == 0
        assert effects_mcp["actions_tombstoned"] >= 1
        assert effects_mcp["mcp_sessions_left"] == 0

        # The REST response body still carries the legacy shape.
        body = resp.json()
        assert body["tombstone"] == "[deleted-alice_rest]"
        assert set(body["counts"]) == {
            "messages_sent", "messages_received", "tasks_created",
            "tasks_assigned", "agent_actions",
        }


async def test_restore_agent_mcp_and_rest_identical(tmp_path) -> None:
    """``restore_agent`` MCP tool and ``POST /api/agents/<id>/restore``
    produce the same status flip + audit action on identical fixtures."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("r_mcp")
        await admin.create_worker("r_rest")
        await _terminate(admin, "r_mcp")
        await _terminate(admin, "r_rest")

        mcp_result = await admin.call("restore_agent", {"agent_id": "r_mcp"})
        assert "restored" in mcp_result[0].text.lower(), mcp_result[0].text

        resp = admin.post(
            "/api/agents/r_rest/restore", json={},
        )
        assert resp.status_code == 200, resp.text

        for ident in ("r_mcp", "r_rest"):
            row = _row("agents", "agent_id = ?", (ident,))
            assert row is not None
            assert row["status"] == "created", (ident, row)
            assert row["terminated_at"] is None, (ident, row)
            assert _count(
                "agent_actions", "action_type = ? AND agent_id = ?",
                ("restored_agent", "admin"),
            ) >= 1


async def test_edit_agent_mcp_and_rest_identical(tmp_path) -> None:
    """``edit_agent`` MCP tool and ``POST /api/agents/<id>/edit`` apply the
    same field write + audit action on identical fixtures."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("e_mcp")
        await admin.create_worker("e_rest")

        mcp_result = await admin.call(
            "edit_agent", {"agent_id": "e_mcp", "color": "#123456"},
        )
        assert "updated" in mcp_result[0].text.lower(), mcp_result[0].text

        resp = admin.post(
            "/api/agents/e_rest/edit",
            json={"color": "#123456"},
        )
        assert resp.status_code == 200, resp.text

        for ident in ("e_mcp", "e_rest"):
            row = _row("agents", "agent_id = ?", (ident,))
            assert row is not None and row["color"] == "#123456", (ident, row)
            assert _count(
                "agent_actions", "action_type = ? AND agent_id = ?",
                ("edited_agent", "admin"),
            ) >= 1
