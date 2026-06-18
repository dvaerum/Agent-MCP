"""Backend tests for the ``agent_role`` field on POST /api/agents.

Phase 2 Wave 2b (prancy-napping-pie §2e). Wave 1a (v5.0.61, PR #182)
added the ``agents.agent_role TEXT NOT NULL DEFAULT 'worker'`` column
with a CHECK constraint admitting ``'worker'`` or ``'manager'`` only.
This PR wires the dashboard's Create-Agent endpoint to accept
``agent_role`` and persist it.

Contract pinned here:
  * POST /api/agents with ``agent_role='manager'`` → agent created with
    ``agent_role='manager'``.
  * POST /api/agents with ``agent_role='worker'`` → agent created with
    ``agent_role='worker'``.
  * POST /api/agents with the field omitted → defaults to
    ``'worker'`` (matches Wave 1a's column default).
  * POST /api/agents with ``agent_role='invalid'`` → 422
    (rejected at the API boundary before any DB write).

Manager-vs-worker privilege enforcement is Wave 3's responsibility;
this PR is strictly about accepting + persisting the field.
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


async def test_create_agent_persists_manager_role(tmp_path) -> None:
    """``agent_role='manager'`` round-trips into the agents row."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": admin.admin_token,
                "agent_id": "mgr-one",
                "agent_role": "manager",
            },
        )
        assert resp.status_code == 200, (
            f"create with agent_role=manager must succeed; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("mgr-one",))
        assert row is not None, "agent row should exist"
        assert row["agent_role"] == "manager", (
            f"agent_role must be persisted; got {row['agent_role']!r}"
        )


async def test_create_agent_persists_worker_role_explicit(tmp_path) -> None:
    """``agent_role='worker'`` (explicit) round-trips into the row."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": admin.admin_token,
                "agent_id": "worker-one",
                "agent_role": "worker",
            },
        )
        assert resp.status_code == 200, (
            f"create with agent_role=worker must succeed; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("worker-one",))
        assert row is not None
        assert row["agent_role"] == "worker"


async def test_create_agent_defaults_to_worker_role(tmp_path) -> None:
    """Omitting ``agent_role`` defaults to ``'worker'`` (matches the
    Wave 1a column default — no behavior change for legacy callers).
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": admin.admin_token,
                "agent_id": "default-worker",
            },
        )
        assert resp.status_code == 200, (
            f"create without agent_role must succeed and default; "
            f"got {resp.status_code} {resp.text!r}"
        )
        row = _row("agents", "agent_id = ?", ("default-worker",))
        assert row is not None
        assert row["agent_role"] == "worker", (
            f"omitted agent_role must default to 'worker'; "
            f"got {row['agent_role']!r}"
        )


async def test_create_agent_rejects_invalid_role(tmp_path) -> None:
    """Unknown role string → 422 BEFORE any DB write.

    The CHECK constraint on the column would also reject this at the DB
    layer, but the API boundary is the right place to reject so the
    response carries a clear validation message instead of bubbling a
    sqlite IntegrityError as a 500.
    """
    async with mcp_session(tmp_path) as admin:
        resp = admin.client.post(
            "/api/agents",
            json={
                "token": admin.admin_token,
                "agent_id": "bad-role",
                "agent_role": "operator",  # not in {worker, manager}
            },
        )
        assert resp.status_code == 422, (
            f"invalid agent_role must 422; got {resp.status_code} {resp.text!r}"
        )
        assert _row("agents", "agent_id = ?", ("bad-role",)) is None, (
            "no agent row may be created when validation rejects the request"
        )
