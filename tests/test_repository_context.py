"""Invariant tests for ContextRepository (PR-W2c).

``project_context`` does not have a separate in-memory cache in
``state.*``; the repo is ORM-aware (delegates to the
``ProjectContext`` SQLAlchemy model directly) and exposes the same
four-invariant contract for uniformity.

- Write-through: ``set_context`` makes the value retrievable via
  ``get_context``.
- Cache-miss: a DB-direct write is reflected on the next repo read.
- EventBus: writes publish to the bus when available (audit-style
  notification — no specific recipient agent_id, so use the
  ``"*"`` broadcast convention).
- disable_cache: no-op context manager for interface uniformity.
"""

from __future__ import annotations

import datetime
import json


from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _insert_context_via_db(key: str, value: str) -> None:
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import ProjectContext

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            ProjectContext(
                context_key=key,
                value=value,
                description="direct",
                created_at=now,
                created_by="admin",
                updated_at=now,
                updated_by="admin",
            )
        )
        session.commit()


def test_set_context_visible_via_repo_read(project_dir, reset_globals):
    """Test A: write-through invariant for set_context."""
    with _make_client(project_dir):
        from agent_mcp.core.repositories import context_repo

        context_repo.set_context(
            context_key="ctx-A",
            value=json.dumps({"k": 1}),
            description="hello",
            updated_by="admin",
        )

        got = context_repo.get_context("ctx-A")
        assert got is not None
        assert got["context_key"] == "ctx-A"
        assert json.loads(got["value"])["k"] == 1


def test_db_direct_insert_visible_via_repo_read(project_dir, reset_globals):
    """Test B: cache-miss path - DB direct insert visible via repo read."""
    with _make_client(project_dir):
        from agent_mcp.core.repositories import context_repo

        _insert_context_via_db("ctx-direct", json.dumps({"hello": "world"}))

        got = context_repo.get_context("ctx-direct")
        assert got is not None
        assert json.loads(got["value"])["hello"] == "world"


def test_set_context_publishes_event(project_dir, reset_globals):
    """Test C: set_context publishes to EventBus when available."""
    captured: list[tuple[str, str, dict]] = []

    class _FakeBus:
        @staticmethod
        def notify(agent_id, event_type, payload):  # noqa: ANN001
            captured.append((agent_id, event_type, payload))

    import sys

    sys.modules["agent_mcp.core.event_bus"] = _FakeBus()  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core.repositories import context_repo

            context_repo.set_context(
                context_key="ctx-bus",
                value="x",
                description=None,
                updated_by="admin",
            )

            assert captured
            agent_id, event_type, _payload = captured[0]
            # Context updates are broadcast events ("*" recipient).
            assert agent_id == "*"
            assert "context" in event_type.lower()
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_disable_cache_is_a_noop_contract(project_dir, reset_globals):
    """Test D: ``disable_cache()`` is a valid context manager."""
    with _make_client(project_dir):
        from agent_mcp.core.repositories import context_repo

        with context_repo.disable_cache():
            context_repo.set_context(
                context_key="ctx-nc",
                value="v",
                description=None,
                updated_by="admin",
            )
            got = context_repo.get_context("ctx-nc")
            assert got is not None
