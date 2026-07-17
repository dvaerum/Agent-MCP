"""Regression: restoring a terminated agent must flip both columns.

Bug report
----------
User restored two workers (backend-dev, ios-app-dev) from the
"terminated" trash view in the dashboard. The workers re-appeared in
the active list visually, but their DB rows still showed
``status='terminated'`` with ``terminated_at`` populated:

    sqlite> SELECT agent_id, status, terminated_at FROM agents;
    backend-dev | terminated | 2026-06-04T05:36:49...
    ios-app-dev | terminated | 2026-06-04T05:36:49...
    admin       | system     | (null)

This regression test pins the two-field invariant:

    after restore:
        agents.status        != 'terminated'   (set to 'created')
        agents.terminated_at IS NULL

Restore is exercised end-to-end by seeding a row with the
'terminated' shape and POSTing /api/agents/<id>/restore — same path
the dashboard's Restore button takes. (We deliberately don't go
through `terminate_agent_tool_impl` first, because the test must
guard the restore step regardless of how the row got terminated:
re-imported DB, manual ops fix, etc.)
"""

from __future__ import annotations

import datetime as _dt
import secrets

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed_terminated_agent(agent_id: str) -> str:
    """Insert a row in `agents` with the exact shape a terminated
    agent has on disk. Returns the token so callers could verify
    `g.active_agents` if needed."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    token = f"tok_{secrets.token_hex(8)}"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agents
                (token, agent_id, capabilities, created_at, status,
                 working_directory, color, updated_at, terminated_at)
            VALUES (?, ?, '[]', ?, 'terminated', '', '#ff0000', ?, ?)
            """,
            (token, agent_id, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def _read_status_and_terminated_at(agent_id: str) -> tuple[str, str | None]:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, terminated_at FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cur.fetchone()
        assert row is not None, f"agents row missing for {agent_id}"
        return row["status"], row["terminated_at"]
    finally:
        conn.close()


async def test_restore_clears_terminated_status_and_timestamp(
    tmp_path,
) -> None:
    """The bug-report invariant: after a restore, the agents row must
    have status != 'terminated' AND terminated_at IS NULL.

    This is the exact assertion the user is checking with their
    `sqlite3 ... "SELECT agent_id, status, terminated_at FROM agents;"`
    query — and the one PR #100's restore endpoint must satisfy."""
    async with mcp_session(tmp_path) as admin:
        _seed_terminated_agent("backend-dev")

        # Sanity check: the seed put us in the bug-report state.
        status, t_at = _read_status_and_terminated_at("backend-dev")
        assert status == "terminated"
        assert t_at is not None

        resp = admin.post(
            "/api/agents/backend-dev/restore",
            json={},
        )
        assert resp.status_code == 200, resp.text

        status_after, t_at_after = _read_status_and_terminated_at(
            "backend-dev"
        )
        assert status_after != "terminated", (
            f"Restore must flip status away from 'terminated'; got "
            f"{status_after!r}"
        )
        assert t_at_after is None, (
            f"Restore must clear terminated_at to NULL; got {t_at_after!r}"
        )


async def test_restore_multiple_workers_clears_both(tmp_path) -> None:
    """User restored both backend-dev AND ios-app-dev. Pin that two
    sequential restores both succeed and both flip the DB."""
    async with mcp_session(tmp_path) as admin:
        _seed_terminated_agent("backend-dev")
        _seed_terminated_agent("ios-app-dev")

        for aid in ("backend-dev", "ios-app-dev"):
            resp = admin.post(
                f"/api/agents/{aid}/restore",
                json={},
            )
            assert resp.status_code == 200, (aid, resp.text)

        for aid in ("backend-dev", "ios-app-dev"):
            status, t_at = _read_status_and_terminated_at(aid)
            assert status != "terminated", (aid, status)
            assert t_at is None, (aid, t_at)
