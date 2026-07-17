"""BL-R13-2: agent restore must reconcile the ``g.agent_working_dirs``
sibling cache.

``restore_agent_api_route`` re-adds the restored agent to
``g.active_agents`` (keyed by token) but historically skipped the SECOND
in-memory view of the working directory: ``g.agent_working_dirs`` (keyed
by agent_id). ``get_working_directory()`` reads ``agent_working_dirs``
FIRST and returns on a non-None hit, so after a restore the file tools +
``get_agent_details`` keep resolving against stale/missing dir data.

This is the restore-path instance of the BL-R11-1 class (which fixed the
EDIT path). RED on origin/main (stale/missing); GREEN after the restore
path mirrors the sibling reconcile.
"""

from __future__ import annotations

import pytest

from agent_mcp.core import globals as g
from agent_mcp.repositories import agent_repo
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _terminate_in_db(agent_id: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE agents SET status = 'terminated', "
            "terminated_at = '2026-01-01T00:00:00' WHERE agent_id = ?",
            (agent_id,),
        )
        conn.commit()
    finally:
        conn.close()


async def test_restore_repopulates_agent_working_dirs_cache(tmp_path) -> None:
    """After restore, get_working_directory() (which reads the
    agent_working_dirs cache first) must reflect the restored agent's
    working_directory — not stale/missing data."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")

        # Give alice a distinctive working directory in the DB.
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE agents SET working_directory = ? WHERE agent_id = ?",
                ("/tmp/alice-wd", "alice"),
            )
            conn.commit()
        finally:
            conn.close()

        # Terminate, then evict the sibling cache the way a terminate /
        # process reload would (BL-R13-2 models the post-terminate state:
        # the workdir cache no longer holds a live entry for the agent).
        _terminate_in_db("alice")
        g.agent_working_dirs.pop("alice", None)

        # Restore.
        resp = admin.post(
            "/api/agents/alice/restore",
            json={},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True, resp.text

        # The cache the file tools actually read must reflect the restored
        # agent's working directory.
        assert g.agent_working_dirs.get("alice") == "/tmp/alice-wd", (
            "restore must repopulate g.agent_working_dirs for the restored "
            "agent"
        )
        assert agent_repo.get_working_directory("alice") == "/tmp/alice-wd"
