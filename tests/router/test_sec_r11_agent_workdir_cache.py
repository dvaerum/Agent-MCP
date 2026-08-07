"""BL-R11-1: agent-edit must reconcile the ``g.agent_working_dirs``
sibling cache when ``working_directory`` changes.

``edit_agent_api_route`` routes field writes through
``agent_repo.update_field(..., connection=cursor)``, whose cursor path
defers the in-memory cache write to the caller. The handler hand-
reconciles ``g.active_agents`` (keyed by token) but historically
skipped the SECOND view of the working directory:
``g.agent_working_dirs`` (keyed by agent_id).

``get_working_directory()`` reads ``agent_working_dirs`` FIRST and
returns on a non-None hit, so the stale cached dir wins over the fresh
``active_agents`` value and the operator's edit silently no-ops for the
agent's relative-path file ops (``file_management_tools`` /
``file_metadata_tools``) and ``get_agent_details``.

RED against origin/main (stale ``/tmp`` retained); GREEN after the
sibling reconcile is added.
"""

from __future__ import annotations

import pytest

from agent_mcp.core import globals as g
from agent_mcp.repositories import agent_repo
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


async def test_edit_working_directory_updates_agent_working_dirs_cache(
    tmp_path,
) -> None:
    """After editing working_directory, get_working_directory() (which
    reads the agent_working_dirs cache first) must return the NEW dir,
    not the stale cached one."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        # Warm the agent_working_dirs cache the way server startup does
        # (server_lifecycle populates it from the DB row). create_worker
        # seeds working_directory="/tmp".
        g.agent_working_dirs["alice"] = "/tmp"
        assert agent_repo.get_working_directory("alice") == "/tmp"

        resp = admin.post(
            "/api/agents/alice/edit",
            json={
                "working_directory": "/tmp/new-wd",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True, resp.text

        # DB is updated (sanity).
        row = _row("agents", "agent_id = ?", ("alice",))
        assert row is not None
        assert row["working_directory"] == "/tmp/new-wd"

        # The cache the file tools actually read must reflect the edit.
        assert g.agent_working_dirs["alice"] == "/tmp/new-wd"
        assert agent_repo.get_working_directory("alice") == "/tmp/new-wd"


async def test_edit_non_workdir_field_leaves_agent_working_dirs_untouched(
    tmp_path,
) -> None:
    """Editing a NON-working_directory field (color) must still succeed
    and must not disturb the agent_working_dirs cache."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")

        g.agent_working_dirs["bob"] = "/tmp"

        resp = admin.post(
            "/api/agents/bob/edit",
            json={"color": "#abcdef"},
        )
        assert resp.status_code == 200, resp.text

        row = _row("agents", "agent_id = ?", ("bob",))
        assert row is not None
        assert row["color"] == "#abcdef"

        # The workdir cache must be exactly as warmed — untouched.
        assert g.agent_working_dirs["bob"] == "/tmp"
