"""Invariant tests for MessageRepository (PR-W2c).

There is no in-memory cache for messages in ``state.*`` today;
``message_repo`` is mostly a thin delegation onto
``agent_mcp.db.actions.agent_messages_db``. The cache invariant
collapses to "the repo's reads agree with DB-direct reads", and the
EventBus + ``disable_cache`` invariants still apply (the latter is a
no-op contract — ``disable_cache()`` must be a valid context manager
even when there is no cache to disable).
"""

from __future__ import annotations

import datetime

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _seed_agent(agent_id: str, token: str | None = None) -> None:
    """agent_messages FKs sender_id/recipient_id to agents.agent_id, so
    every message test needs the referenced agent rows to exist."""
    from agent_mcp.core.repositories import agent_repo

    agent_repo.create_agent(
        token=token or f"tok-{agent_id}",
        agent_id=agent_id,
        capabilities=[],
        status="active",
        working_directory="/tmp/wd",
        color="#abcdef",
    )


def test_create_message_visible_via_repo_read(project_dir, reset_globals):
    """Test A/B combined: write through repo, read by id, agrees with DB."""
    with _make_client(project_dir):
        from agent_mcp.core.repositories import message_repo
        from agent_mcp.db.actions.agent_messages_db import get_message_by_id

        _seed_agent("worker-A")

        now = datetime.datetime.now().isoformat()
        ok = message_repo.create_message(
            message_id="msg-1",
            sender_id="admin",
            recipient_id="worker-A",
            message_content="hello",
            message_type="direct",
            priority="normal",
            timestamp=now,
        )
        assert ok

        got = message_repo.get_message("msg-1")
        assert got is not None
        assert got["recipient_id"] == "worker-A"

        # Repo agrees with the db.actions layer.
        from_db = get_message_by_id("msg-1")
        assert from_db is not None
        assert from_db["message_content"] == got["message_content"]


def test_create_message_publishes_event(project_dir, reset_globals):
    """Test C: writes publish to EventBus when available."""
    captured: list[tuple[str, str, dict]] = []

    class _FakeBus:
        @staticmethod
        def notify(agent_id, event_type, payload):  # noqa: ANN001
            captured.append((agent_id, event_type, payload))

    import sys

    sys.modules["agent_mcp.core.event_bus"] = _FakeBus()  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core.repositories import message_repo

            _seed_agent("worker-bus")
            captured.clear()  # ignore the agent.created event from the seed

            now = datetime.datetime.now().isoformat()
            message_repo.create_message(
                message_id="msg-bus-1",
                sender_id="admin",
                recipient_id="worker-bus",
                message_content="ping",
                message_type="direct",
                priority="normal",
                timestamp=now,
            )

            assert captured
            agent_id, event_type, _payload = captured[0]
            assert agent_id == "worker-bus"
            assert "message" in event_type.lower()
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_disable_cache_is_a_noop_contract(project_dir, reset_globals):
    """Test D: ``disable_cache()`` is a valid context manager even
    when there is no in-memory cache - keeps the repo interface
    uniform across the four repos."""
    with _make_client(project_dir):
        from agent_mcp.core.repositories import message_repo

        _seed_agent("worker-nc")

        with message_repo.disable_cache():
            now = datetime.datetime.now().isoformat()
            ok = message_repo.create_message(
                message_id="msg-nc",
                sender_id="admin",
                recipient_id="worker-nc",
                message_content="nc",
                message_type="direct",
                priority="normal",
                timestamp=now,
            )
            assert ok
            got = message_repo.get_message("msg-nc")
            assert got is not None
