"""Backend tests for POST /api/agents/<id>/edit.

The dashboard's Agents page Edit-icon needs a way to mutate the
admin-editable agent fields: `color`, `working_directory`. This PR
adds a minimal admin-only REST endpoint that updates the row using
the existing `update_agent_db_field` helper.

Contract:
  - Method: POST
  - URL:    /api/agents/<id>/edit
  - Body:   {"token": admin_token, "color"?: str,
                                  "working_directory"?: str}
  - Auth:   admin token required (403 otherwise)
  - 404 when the agent_id doesn't exist
  - 200 + updated row echoed back on success
  - Omitting all editable fields → 400 (nothing to update)

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
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


# -------------------- happy path -------------------------------------


async def test_edit_updates_color(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/agents/alice/edit",
            json={"color": "#abcdef"},
        )
        assert resp.status_code == 200, resp.text

        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["color"] == "#abcdef"


async def test_edit_updates_working_directory(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/agents/alice/edit",
            json={"working_directory": "/workspace/alice"},
        )
        assert resp.status_code == 200, resp.text

        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["working_directory"] == "/workspace/alice"


async def test_edit_updates_multiple_fields_at_once(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/agents/alice/edit",
            json={
                "color": "#deadbe",
                "working_directory": "/home/alice",
            },
        )
        assert resp.status_code == 200, resp.text

        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["color"] == "#deadbe"
        assert row["working_directory"] == "/home/alice"


# -------------------- auth + validation ------------------------------


async def test_edit_rejects_worker_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        # Worker bearer: exercises the operator-tier gate (a non-operator
        # bearer must be rejected), not merely the no-auth 401 path.
        resp = admin.client.post(
            "/api/agents/alice/edit",
            json={"color": "#000000"},
            headers={"Authorization": f"Bearer {bob.token}"},
        )
        assert resp.status_code in (401, 403), resp.text


async def test_edit_404_when_agent_missing(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/agents/nonexistent/edit",
            json={"color": "#000000"},
        )
        assert resp.status_code == 404, resp.text


async def test_edit_400_when_no_editable_fields(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/agents/alice/edit",
            json={},
        )
        assert resp.status_code == 400, resp.text


async def test_edit_rejects_non_whitelisted_fields(tmp_path) -> None:
    """Sending `status` or `agent_id` (not in the whitelist) must not
    touch the row — only color/working_directory are
    editable through this endpoint."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        resp = admin.post(
            "/api/agents/alice/edit",
            json={
                "status": "terminated",
                "agent_id": "renamed",
            },
        )
        # Either 400 (no editable fields supplied) or 200 (silently ignored).
        # Either way, the agents row must NOT have been mutated.
        assert resp.status_code in (200, 400), resp.text
        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None, (
            "alice row must still exist with the original agent_id"
        )
        assert row["status"] == "active"
