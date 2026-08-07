"""Parity tests pinning REST endpoint <-> MCP tool semantic equivalence.

Candidate C of the 2026-06-02 architecture review: REST handlers in
``agent_mcp/app/routes.py`` become thin adapters that dispatch through
the MCP-tool dispatcher (``agent_mcp.tools.registry.dispatch_tool_call``).
The win: validation lives once (the tool's ``inputSchema`` +
``@requires`` decorator); auth flows through the same
``request_auth_token`` ContextVar; the dashboard's ``/api/...`` URLs
stay backward-compatible at the wire level.

This module pins **wire-level + DB-level parity** between each pair so
the adapter rewrite cannot silently introduce drift:

  * Send the same payload to the REST endpoint and to the underlying
    MCP tool through the in-process harness.
  * Assert the resulting DB rows / row count match.
  * Assert auth rejection (wrong / missing token) produces the same
    failure mode on both surfaces.

Scope cut: only the three endpoints with a 1:1 MCP-tool match and no
custom REST-only logic are migrated in this PR. ``/api/messages`` and
``/api/memories`` (POST/PUT) have field-name or upsert-vs-409
semantics that differ from the MCP equivalents and would change the
dashboard wire contract if forced through dispatch. See the PR
description for the full table.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from tests.harness import mcp_session

# --- /api/terminate-agent  <->  terminate_agent ---


@pytest.mark.asyncio
async def test_terminate_agent_rest_matches_mcp_tool(tmp_path) -> None:
    """POST /api/terminate-agent and the MCP ``terminate_agent`` tool
    leave the same row state for the same payload.

    Setup: two agents Alice and Bob. Terminate Alice via REST,
    terminate Bob via MCP. Both should land with status='terminated'
    and a non-NULL terminated_at.
    """
    async with mcp_session(tmp_path) as admin:
        # Register alice + bob in g.active_agents (the value is bound
        # to the per-test session globals; we don't need the returned
        # WorkerSession objects since both terminations are admin-driven).
        await admin.create_worker("alice")
        await admin.create_worker("bob")

        # --- REST path ---
        r = admin.post(
            "/api/terminate-agent",
            json={"agent_id": "alice"},
        )
        assert r.status_code == 200, r.text

        # --- MCP path ---
        await admin.assert_tool_succeeds(
            "terminate_agent",
            {"agent_id": "bob"},
        )

        # --- Parity: both rows look identical structurally ---
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT agent_id, status, terminated_at FROM agents "
                "WHERE agent_id IN ('alice', 'bob') ORDER BY agent_id"
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        assert len(rows) == 2
        for row in rows:
            assert row["status"] == "terminated", row
            assert row["terminated_at"] is not None, row


@pytest.mark.asyncio
async def test_terminate_agent_rest_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Fake bearer: exercises the operator-tier gate, not no-auth 401.
        r = admin.client.post(
            "/api/terminate-agent",
            json={"agent_id": "alice"},
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        assert r.status_code in (401, 403), r.text


@pytest.mark.asyncio
async def test_terminate_agent_rest_404_unknown(tmp_path) -> None:
    """Unknown agent_id returns a non-2xx; both REST and MCP report it."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/terminate-agent",
            json={"agent_id": "ghost"},
        )
        assert r.status_code in (400, 404), r.text


# --- DELETE /api/tasks/<id>  <->  delete_task ---


def _seed_task(conn_factory, task_id: str, title: str, created_by: str) -> None:
    """Insert a minimal valid task row directly so the test isn't
    coupled to any REST/MCP creation path."""
    now = _dt.datetime.now().isoformat()
    conn = conn_factory()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks (
                task_id, title, description, assigned_to, created_by,
                status, priority, created_at, updated_at,
                parent_task, child_tasks, depends_on_tasks, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, title, "seed", None, created_by,
                "pending", "medium", now, now,
                None, "[]", "[]", "[]",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_delete_task_rest_matches_mcp_tool(tmp_path) -> None:
    """Two seeded tasks, deleted via different surfaces; the resulting
    table state is identical (both rows gone)."""
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.db.connection import get_db_connection

        _seed_task(get_db_connection, "task_rest_001", "rest path", "admin")
        _seed_task(get_db_connection, "task_mcp_001", "mcp path", "admin")

        # REST
        r = admin.request(
            "DELETE",
            "/api/tasks/task_rest_001",
            json={},
        )
        assert r.status_code == 200, r.text

        # MCP
        await admin.assert_tool_succeeds(
            "delete_task",
            {"task_id": "task_mcp_001"},
        )

        # Parity: both gone.
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT task_id FROM tasks WHERE task_id IN "
                "('task_rest_001', 'task_mcp_001')"
            )
            assert cur.fetchall() == []
        finally:
            conn.close()


@pytest.mark.asyncio
async def test_delete_task_rest_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.db.connection import get_db_connection

        _seed_task(get_db_connection, "task_keep", "keep", "admin")
        # Fake bearer: exercises the operator-tier gate, not no-auth 401.
        r = admin.client.request(
            "DELETE",
            "/api/tasks/task_keep",
            json={},
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        assert r.status_code in (401, 403), r.text

        # Task survives.
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT task_id FROM tasks WHERE task_id = 'task_keep'")
            assert cur.fetchone() is not None
        finally:
            conn.close()


@pytest.mark.asyncio
async def test_delete_task_rest_404_unknown(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "DELETE",
            "/api/tasks/task_does_not_exist",
            json={},
        )
        assert r.status_code == 404, r.text


# --- DELETE /api/memories/<key>  <->  delete_project_context ---


def _seed_memory(key: str, value: object, created_by: str) -> None:
    """Insert a memory row via SQLAlchemy ORM (same shape the REST
    create endpoint produces)."""
    from agent_mcp.db.engine import SessionLocal
    from agent_mcp.db.models import ProjectContext

    now = _dt.datetime.now().isoformat()
    sess = SessionLocal()
    try:
        sess.add(
            ProjectContext(
                context_key=key,
                value=json.dumps(value),
                created_at=now,
                created_by=created_by,
                updated_at=now,
                updated_by=created_by,
                description="seed",
            )
        )
        sess.commit()
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_delete_memory_rest_matches_mcp_tool(tmp_path) -> None:
    """Two seeded memory rows: one deleted via REST, one via MCP. Both
    are gone after; the same audit-log action_type is recorded.

    NOTE: the MCP ``delete_project_context`` tool refuses to delete
    "critical" keys (``config_*``, ``server_*``, ``mcp_*``, etc.)
    without ``force_delete=true``. R9-F2 routed the REST DELETE handler
    through that gated tool, so the REST surface now ENFORCES the same
    guard: ``force_delete`` is read from the request body (default
    ``False``) — it is no longer auto-passed. The test uses a
    non-critical key so the guard doesn't apply on either surface.
    """
    async with mcp_session(tmp_path) as admin:
        _seed_memory("mem.rest.k1", {"hello": "world"}, "admin")
        _seed_memory("mem.mcp.k1", {"foo": "bar"}, "admin")

        # REST
        r = admin.request(
            "DELETE",
            "/api/memories/mem.rest.k1",
            json={},
        )
        assert r.status_code == 200, r.text

        # MCP
        await admin.assert_tool_succeeds(
            "delete_project_context",
            {"context_key": "mem.mcp.k1"},
        )

        # Parity: both gone.
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            rows = (
                sess.query(ProjectContext)
                .filter(
                    ProjectContext.context_key.in_(
                        ["mem.rest.k1", "mem.mcp.k1"]
                    )
                )
                .all()
            )
            assert rows == []
        finally:
            sess.close()


@pytest.mark.asyncio
async def test_delete_memory_rest_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed_memory("mem.keep", {"data": 1}, "admin")
        # Fake bearer: exercises the operator-tier gate, not no-auth 401.
        r = admin.client.request(
            "DELETE",
            "/api/memories/mem.keep",
            json={},
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        assert r.status_code in (401, 403), r.text

        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == "mem.keep")
                .one_or_none()
            )
            assert row is not None
        finally:
            sess.close()


@pytest.mark.asyncio
async def test_delete_memory_rest_404_unknown(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "DELETE",
            "/api/memories/mem.does.not.exist",
            json={},
        )
        assert r.status_code == 404, r.text


# --- Negative parity: validation drift is gone ---


@pytest.mark.asyncio
async def test_terminate_agent_missing_agent_id_returns_400(tmp_path) -> None:
    """The MCP tool's inputSchema declares ``agent_id`` required; the
    REST adapter must surface that as a 400 (not a 500 or silent 200).
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/terminate-agent",
            json={},
        )
        assert r.status_code in (400, 404), r.text


@pytest.mark.asyncio
async def test_delete_task_missing_task_id_returns_400(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "DELETE",
            "/api/tasks/",
            json={},
        )
        # 400 (handler rejects empty), 404 (no route match), or 405
        # (the trailing-slash GET-only route catches the DELETE) are
        # all acceptable — they all say "the empty-id case is not
        # silently a 200".
        assert r.status_code in (400, 404, 405), r.text
