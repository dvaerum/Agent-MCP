"""Round-4 security fix BL-R4-2 — purge must clean session rows.

`purge_agent_api_route` tombstones `agent_messages`, `tasks`, and
`agent_actions`, then `DELETE`s the `agents` row LAST. It historically
left `mcp_sessions.agent_id` and `claude_code_sessions.agent_id`
untouched — both declared as FKs to `agents.agent_id` by migrations
0007/0008.

On a migration-built DB (FKs enforced) a referencing session row at
delete time makes the final `DELETE FROM agents` raise
`FOREIGN KEY constraint failed`, rolling back the ENTIRE purge
(availability bug). On a `create_all()` DB the FK isn't enforced (the
ORM omits `ForeignKey()`), so at most orphaned rows remain.

Either way, the observable contract is: a purge deletes the purged
agent's session rows. These tests pin that contract — the purge must
succeed AND leave no session rows referencing the deleted agent.
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ---- helpers ---------------------------------------------------------


async def _terminate(admin, agent_id: str) -> None:
    result = await admin.call("terminate_agent", {"agent_id": agent_id})
    text = result[0].text
    assert "terminated" in text.lower(), f"terminate failed: {text}"


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


def _insert_mcp_session(agent_id: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    session_id = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
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


def _insert_claude_code_session(agent_id: str) -> str:
    from agent_mcp.db.connection import get_db_connection

    session_id = secrets.token_hex(8)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO claude_code_sessions (session_id, pid, parent_pid, "
            "first_detected, last_activity, working_directory, agent_id, "
            "status) VALUES (?, ?, ?, ?, ?, NULL, ?, 'detected')",
            (session_id, 4242, 1, now, now, agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


async def _purge(admin, agent_id: str):
    return admin.request(
        "DELETE",
        f"/api/agents/{agent_id}",
        params={"cascade": "true"},
        json={},
    )


# ---- tests -----------------------------------------------------------


async def test_purge_deletes_mcp_session_rows(tmp_path) -> None:
    """A purged agent's `mcp_sessions` rows are deleted by the cascade so
    no orphan (or FK failure on an enforced DB) survives the purge."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        _insert_mcp_session("alice")
        _insert_mcp_session("alice")
        assert _count("mcp_sessions", "agent_id = ?", ("alice",)) == 2

        resp = await _purge(admin, "alice")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True

        # agents row gone AND no dangling session rows.
        assert _count("mcp_sessions", "agent_id = ?", ("alice",)) == 0, (
            "purge must delete the purged agent's mcp_sessions rows"
        )


async def test_purge_deletes_claude_code_session_rows(tmp_path) -> None:
    """A purged agent's `claude_code_sessions` rows are deleted too."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        _insert_claude_code_session("alice")
        assert _count(
            "claude_code_sessions", "agent_id = ?", ("alice",)
        ) == 1

        resp = await _purge(admin, "alice")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True

        assert _count(
            "claude_code_sessions", "agent_id = ?", ("alice",)
        ) == 0, (
            "purge must delete the purged agent's claude_code_sessions rows"
        )


async def test_purge_leaves_other_agents_sessions_intact(tmp_path) -> None:
    """Only the purged agent's session rows go — a bystander agent's
    sessions must survive."""
    from tests.harness import seed_agent_rows

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        seed_agent_rows("bob")
        await _terminate(admin, "alice")

        _insert_mcp_session("alice")
        _insert_mcp_session("bob")
        _insert_claude_code_session("bob")

        resp = await _purge(admin, "alice")
        assert resp.status_code == 200, resp.text

        assert _count("mcp_sessions", "agent_id = ?", ("alice",)) == 0
        assert _count("mcp_sessions", "agent_id = ?", ("bob",)) == 1
        assert _count(
            "claude_code_sessions", "agent_id = ?", ("bob",)
        ) == 1


async def test_purge_without_sessions_still_succeeds(tmp_path) -> None:
    """Regression: purging an agent that has no session rows still works
    (the session cleanup is a no-op, not an error)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await _terminate(admin, "alice")

        resp = await _purge(admin, "alice")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True
