"""Cache-vs-DB invariant tests for AgentRepository (PR-W2c).

The repo owns the in-memory ``state.active_agents`` +
``state.agent_working_dirs`` caches alongside the
``agents`` table reads/writes. Same four invariants as TaskRepository:
write-through, cache-miss, EventBus, disable_cache.

``state.active_agents`` is keyed by **token**, not agent_id. The
repo's getters take agent_id (the natural key) and look up by either
identifier, but cache updates land under the token to keep parity
with the legacy lookup pattern.
"""

from __future__ import annotations

import datetime

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _insert_agent_via_db(
    *, agent_id: str, token: str, status: str = "active"
) -> None:
    """Direct DB insert that bypasses the repo."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
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
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
            )
        )
        session.commit()


def test_create_agent_updates_cache_immediately(project_dir, reset_globals):
    """Test A: write-through invariant for create_agent."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import agent_repo

        agent_repo.create_agent(
            token="tok-A",
            agent_id="worker-A",
            capabilities=["python"],
            status="created",
            working_directory="/tmp/worker-A",
            color="#112233",
        )

        # Cache reflects new value immediately - keyed by token.
        assert "tok-A" in state.active_agents
        assert state.active_agents["tok-A"]["agent_id"] == "worker-A"
        assert state.agent_working_dirs["worker-A"] == "/tmp/worker-A"

        # Repo getters work too.
        got = agent_repo.get_agent_by_id("worker-A")
        assert got is not None
        assert got["agent_id"] == "worker-A"


def test_db_direct_insert_visible_on_next_repo_read(project_dir, reset_globals):
    """Test B: cache-miss path falls through to DB."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import agent_repo

        assert "tok-direct" not in state.active_agents

        _insert_agent_via_db(agent_id="worker-direct", token="tok-direct")

        # Cache still empty (we bypassed repo) ...
        assert "tok-direct" not in state.active_agents
        # ... repo getter falls through to DB.
        got = agent_repo.get_agent_by_id("worker-direct")
        assert got is not None
        assert got["agent_id"] == "worker-direct"
        # Warm-on-miss: subsequent reads can be cache hits.
        assert "tok-direct" in state.active_agents


def test_update_agent_status_publishes_event(
    project_dir, reset_globals
):
    """Test C: status update publishes to EventBus when available."""
    captured: list[tuple[str, str, dict]] = []

    class _FakeBus:
        @staticmethod
        def notify(agent_id, event_type, payload):  # noqa: ANN001
            captured.append((agent_id, event_type, payload))

    import sys

    sys.modules["agent_mcp.core.event_bus"] = _FakeBus()  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core.repositories import agent_repo

            agent_repo.create_agent(
                token="tok-evt",
                agent_id="worker-evt",
                capabilities=[],
                status="active",
                working_directory="/tmp/wd",
                color="#000000",
            )
            captured.clear()

            agent_repo.update_agent_field(
                agent_id="worker-evt",
                field_name="status",
                new_value="terminated",
            )

            assert captured, "EventBus must receive agent-status events"
            agent_id, event_type, _payload = captured[0]
            assert agent_id == "worker-evt"
            assert "agent" in event_type.lower()
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_disable_cache_skips_in_memory(project_dir, reset_globals):
    """Test D: ``disable_cache()`` bypasses the cache."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.core.repositories import agent_repo

        with agent_repo.disable_cache():
            agent_repo.create_agent(
                token="tok-nc",
                agent_id="worker-nc",
                capabilities=[],
                status="active",
                working_directory="/tmp/nc",
                color="#abc123",
            )
            assert "tok-nc" not in state.active_agents
            assert "worker-nc" not in state.agent_working_dirs

            got = agent_repo.get_agent_by_id("worker-nc")
            assert got is not None
            assert got["agent_id"] == "worker-nc"

            assert "tok-nc" not in state.active_agents
