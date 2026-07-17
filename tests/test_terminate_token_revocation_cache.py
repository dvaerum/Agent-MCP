"""Security regression: terminated agents must never re-enter the auth cache.

Finding (HIGH): agent-termination token revocation is bypassable via
auth-cache repopulation.

The ``/mcp`` auth gate in ``app.main_app`` is cache-only against
``state.active_agents`` and trusts the invariant "the cache holds only
non-terminated rows". Identity-resolution helpers
(``core.auth.get_agent_id`` -> ``agent_repo.get_agent_by_token``) broke
that invariant: on a cache miss they wrote the DB row into
``state.active_agents`` UNCONDITIONALLY, re-inserting terminated tokens
so they authenticated again.

Exploit precondition: one active agent bearer plus knowledge of a
terminated token. The active agent references the terminated token
(e.g. via ``assign_task {agent_token: T}``); ``get_agent_id(T)`` runs
the ownership check but has already repopulated the cache -> the
terminated bearer is reactivated.

These tests pin the fix: DB reads for audit may still see terminated
rows, but the ``state.active_agents`` cache-WRITE must exclude them.
"""

from __future__ import annotations

import datetime
import secrets

import pytest
from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient

from tests.harness import mcp_session


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _insert_agent_via_db(
    *, agent_id: str, token: str, status: str = "active",
    agent_role: str = "worker",
) -> None:
    """Direct DB insert that bypasses the repo (and thus the cache)."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    terminated_at = now if status == "terminated" else None
    with get_session() as session:
        session.add(
            Agent(
                token=token,
                agent_id=agent_id,
                capabilities="[]",
                created_at=now,
                status=status,
                current_task=None,
                working_directory="/tmp/wd",
                color="#abcdef",
                terminated_at=terminated_at,
                updated_at=now,
                aoe_session_id=None,
                agent_role=agent_role,
            )
        )
        session.commit()


def test_get_agent_by_token_does_not_cache_terminated_row(
    project_dir, reset_globals,
):
    """Cache-first token lookup must NOT warm the cache for a terminated
    row. The row may still be returned (audit/attribution), but the
    ``state.active_agents`` write is the auth gate and must exclude it."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _insert_agent_via_db(
            agent_id="ghost", token="tok-terminated", status="terminated",
        )
        assert "tok-terminated" not in state.active_agents

        row = agent_repo.get_by_token("tok-terminated")

        # Audit read may see the row...
        assert row is not None
        assert row["status"] == "terminated"
        # ...but the auth cache must NOT be repopulated.
        assert "tok-terminated" not in state.active_agents


def test_get_agent_by_id_does_not_cache_terminated_row(
    project_dir, reset_globals,
):
    """Same invariant on the agent_id cache-first path (reached via the
    assign_task ownership check)."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _insert_agent_via_db(
            agent_id="ghost2", token="tok-terminated-2", status="terminated",
        )

        row = agent_repo.get_by_id("ghost2")

        assert row is not None
        assert row["status"] == "terminated"
        assert "tok-terminated-2" not in state.active_agents


def test_get_agent_id_does_not_reactivate_terminated_bearer(
    project_dir, reset_globals,
):
    """Exploit path: resolving a terminated token via ``core.auth.
    get_agent_id`` (as the assign_task authorization does) must not
    re-insert it into the auth cache. Otherwise ``/mcp`` (cache-only)
    would admit the terminated bearer again."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core import auth as core_auth

        _insert_agent_via_db(
            agent_id="ghost3", token="tok-terminated-3", status="terminated",
        )

        # An active agent references the terminated token; get_agent_id
        # resolves it (returns the agent_id for audit) but must NOT warm
        # the cache.
        resolved = core_auth.get_agent_id("tok-terminated-3")

        # It may resolve for audit/attribution...
        assert resolved in (None, "ghost3")
        # ...but the /mcp cache-only gate must still reject the bearer.
        assert "tok-terminated-3" not in state.active_agents


def test_active_agent_token_still_caches(project_dir, reset_globals):
    """Guard: the fix must not break the happy path. A non-terminated
    token is still warmed into the cache on read."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _insert_agent_via_db(
            agent_id="live", token="tok-live", status="active",
        )
        assert "tok-live" not in state.active_agents

        row = agent_repo.get_by_token("tok-live")

        assert row is not None
        assert "tok-live" in state.active_agents


def _seed_terminated_agent(
    agent_id: str, *, agent_role: str = "worker",
) -> str:
    """Insert a terminated agents row directly (bypassing the repo).
    Returns the token."""
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    token = f"tok_{secrets.token_hex(8)}"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agents
                (token, agent_id, capabilities, created_at, status,
                 working_directory, color, updated_at, terminated_at,
                 agent_role)
            VALUES (?, ?, '[]', ?, 'terminated', '/tmp/wd', '#ff0000',
                    ?, ?, ?)
            """,
            (token, agent_id, now, now, now, agent_role),
        )
        conn.commit()
    finally:
        conn.close()
    return token


@pytest.mark.asyncio
async def test_restore_repopulates_cache_with_agent_role(tmp_path) -> None:
    """Restore must rebuild the full cache row including ``agent_role``.

    A restored manager that transiently resolves to worker caps (because
    the re-added cache entry omitted ``agent_role``) is a privilege
    downgrade until the next lifespan reload. Pin the full-row rebuild.
    """
    from agent_mcp.core import globals as _g

    async with mcp_session(tmp_path) as admin:
        token = _seed_terminated_agent("mgr", agent_role="manager")

        resp = admin.post(
            "/api/agents/mgr/restore",
            json={},
        )
        assert resp.status_code == 200, resp.text

        cached = _g.active_agents.get(token)
        assert cached is not None, "restore must re-add the agent to the cache"
        assert cached.get("agent_role") == "manager", (
            "restored cache entry must carry agent_role so a manager "
            "keeps manager capabilities before the next reload"
        )
