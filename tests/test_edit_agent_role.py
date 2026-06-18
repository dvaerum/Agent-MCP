"""Backend tests for ``agent_role`` on POST /api/agents/<id>/edit.

Phase 2 Wave 2b (prancy-napping-pie §2e). The edit endpoint already
whitelists ``capabilities`` / ``color`` / ``working_directory`` /
``aoe_session_id`` / ``auto_event_loop``; this PR extends the
whitelist with ``agent_role`` so the dashboard's Edit-Agent modal can
promote a worker to manager (or demote in the rare case of mistake).

Contract pinned here:
  * POST /api/agents/<id>/edit with ``agent_role='manager'`` →
    200 + row's ``agent_role`` flips to ``'manager'``.
  * POST /api/agents/<id>/edit with ``agent_role='worker'`` →
    200 + row's ``agent_role`` flips back to ``'worker'``.
  * POST /api/agents/<id>/edit with ``agent_role='invalid'`` →
    422, row unchanged.

The default-tier worker created by ``admin.create_worker`` has
``agent_role='worker'`` (column default); the promote test starts from
that state.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _row(table: str, where_sql: str, params: tuple) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
        r = cursor.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


async def test_edit_promotes_worker_to_manager(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Sanity: the default is worker.
        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["agent_role"] == "worker"

        resp = admin.client.post(
            "/api/agents/alice/edit",
            json={"token": admin.admin_token, "agent_role": "manager"},
        )
        assert resp.status_code == 200, (
            f"edit with agent_role=manager must succeed; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["agent_role"] == "manager", (
            f"agent_role must be persisted; got {row['agent_role']!r}"
        )


async def test_edit_demotes_manager_back_to_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        # Promote first.
        resp = admin.client.post(
            "/api/agents/bob/edit",
            json={"token": admin.admin_token, "agent_role": "manager"},
        )
        assert resp.status_code == 200, resp.text
        # Then demote.
        resp = admin.client.post(
            "/api/agents/bob/edit",
            json={"token": admin.admin_token, "agent_role": "worker"},
        )
        assert resp.status_code == 200, resp.text
        row = _row("agents", "agent_id = ?", ("bob",))
        assert row is not None
        assert row["agent_role"] == "worker"


async def test_edit_rejects_invalid_agent_role(tmp_path) -> None:
    """Unknown role string → 422; the row's existing agent_role is left
    untouched (the CHECK constraint would also reject at the DB layer,
    but the API boundary is the right place to reject)."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("charlie")
        resp = admin.client.post(
            "/api/agents/charlie/edit",
            json={"token": admin.admin_token, "agent_role": "operator"},
        )
        assert resp.status_code == 422, (
            f"invalid agent_role on edit must 422; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("charlie",))
        assert row is not None
        assert row["agent_role"] == "worker", (
            "rejected edit must not have mutated agent_role"
        )
