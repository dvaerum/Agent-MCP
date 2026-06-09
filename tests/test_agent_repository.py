"""Contract tests for the class-based ``AgentRepository`` (PR follow-up to #146).

PR #137 introduced module-of-functions repositories under
``agent_mcp.core.repositories``. PR #146 promoted ``TaskRepository`` to
a real class on ``agent_mcp.repositories``. This file pins the same
contract for the Agent concept.

What this test file pins:

* The singleton exists at ``agent_mcp.repositories.agent_repo`` after
  application lifespan startup, and points at an ``AgentRepository``
  instance.
* Every method preserves the wire-equivalent semantics of the legacy
  module-of-functions surface (return shapes, cache invariants —
  ``state.active_agents`` keyed by token + ``state.agent_working_dirs``
  keyed by agent_id — EventBus publishing) so call-site migrations are
  mechanical.
* ``terminate`` is net-new on the class form (parallel to
  ``TaskRepository.delete``): it centralises the "status →
  'terminated', evict both caches, publish ``agent.terminated``"
  ritual that admin_tools currently spells out by hand in a
  multi-table transaction.

These tests fail on ``main`` because:

* ``agent_mcp.repositories.agent_repository`` (the class module) does
  not yet exist.
* ``agent_mcp.repositories.agent_repo`` (the lifespan singleton) is
  not exposed from the top-level repositories package.
* ``AgentRepository.terminate`` is net-new.
"""

from __future__ import annotations

import datetime
import sys

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


# --- Helpers -------------------------------------------------------------


def _make_client(project_dir):
    """Build the in-process app + TestClient.

    Using a fresh client per test means each call runs through the
    full lifespan startup (which is what wires the singleton).
    """
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _seed_agent(
    agent_id: str,
    *,
    token: str | None = None,
    status: str = "active",
    working_directory: str = "/tmp/seed",
    color: str = "#abcdef",
    capabilities_json: str = "[]",
):
    """Insert an agent via the ORM path, bypassing the repo.

    The repo class under test must observe this row when callers ask
    for it — proves the read methods fall through to the DB rather
    than returning only what passed through ``create``.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=token or f"tok-{agent_id}",
                agent_id=agent_id,
                capabilities=capabilities_json,
                created_at=now,
                status=status,
                current_task=None,
                working_directory=working_directory,
                color=color,
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
            )
        )
        session.commit()


class _CapturingBus:
    """Drop-in replacement for ``agent_mcp.core.event_bus``.

    Captures every ``(agent_id, event_type, payload)`` tuple so a
    test can assert exactly one publish per write.
    """

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def notify(self, agent_id, event_type, payload):  # noqa: D401, ANN001
        self.events.append((agent_id, event_type, payload or {}))


# --- Singleton + lifespan wiring ----------------------------------------


def test_agent_repo_singleton_is_agentrepository_instance(
    project_dir, reset_globals,
):
    """``agent_mcp.repositories.agent_repo`` resolves to a class instance.

    The plan locks "module singletons, lifespan-owned" — so the
    attribute access shape is ``from agent_mcp.repositories import
    agent_repo`` and the value is an instance, not a module.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        from agent_mcp.repositories.agent_repository import AgentRepository

        assert isinstance(agent_repo, AgentRepository), (
            "agent_repo must be an AgentRepository instance after lifespan "
            "startup so call sites can rely on the class-based contract"
        )


# --- Read interface ------------------------------------------------------


def test_get_by_id_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_id`` returns the same dict shape legacy callers expect."""
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-getbyid", token="tok-getbyid", status="active")

        row = agent_repo.get_by_id("agent-getbyid")
        assert row is not None
        assert row["agent_id"] == "agent-getbyid"
        assert row["token"] == "tok-getbyid"
        # Capabilities list is deserialised — preserves legacy projection.
        assert row["capabilities"] == []


def test_get_by_id_returns_none_when_missing(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        assert agent_repo.get_by_id("does-not-exist") is None


def test_get_by_token_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_token`` is the auth hot-path lookup. Cache-first read."""
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-tok", token="tok-bearer-123", status="active")

        row = agent_repo.get_by_token("tok-bearer-123")
        assert row is not None
        assert row["agent_id"] == "agent-tok"


def test_list_active_excludes_terminated(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("alive-1", token="tok-alive-1", status="active")
        _seed_agent("alive-2", token="tok-alive-2", status="created")
        _seed_agent("dead-1", token="tok-dead-1", status="terminated")

        rows = agent_repo.list_active()
        ids = {row["agent_id"] for row in rows}
        assert "alive-1" in ids
        assert "alive-2" in ids
        assert "dead-1" not in ids


# --- Write interface: create --------------------------------------------


def test_create_returns_dict_and_updates_caches_and_publishes(
    project_dir, reset_globals,
):
    """``create`` is the single seam for new agents.

    Contract:
      1. Returns the freshly-created dict (not just bool).
      2. ``state.active_agents`` (keyed by token) carries the row.
      3. ``state.agent_working_dirs`` (keyed by agent_id) carries the
         working directory — they're two views over the same row and
         must stay in sync.
      4. EventBus sees exactly one publish for the new agent.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            entity = agent_repo.create(
                token="tok-create",
                agent_id="agent-create",
                capabilities=["python"],
                status="created",
                working_directory="/tmp/agent-create",
                color="#112233",
            )

            assert entity["agent_id"] == "agent-create"
            assert entity["token"] == "tok-create"
            assert entity["capabilities"] == ["python"]

            # Both caches reflect the new agent.
            assert "tok-create" in state.active_agents, (
                "create must populate active_agents (single ownership of "
                "cache+DB invariant)"
            )
            assert state.agent_working_dirs.get("agent-create") == (
                "/tmp/agent-create"
            ), (
                "create must populate agent_working_dirs in lockstep with "
                "active_agents (two views over the same row)"
            )

            # Exactly one publish for the create.
            create_events = [
                e for e in bus.events if "agent" in e[1] and "created" in e[1]
            ]
            assert len(create_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


# --- Write interface: update_field --------------------------------------


def test_update_field_success_emits_event_and_updates_cache(
    project_dir, reset_globals,
):
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            _seed_agent("agent-upd", token="tok-upd", status="created")
            # Prime the cache.
            agent_repo.get_by_token("tok-upd")
            bus.events.clear()

            result = agent_repo.update_field(
                "agent-upd", "status", "active",
            )

            assert result is not None
            assert result["status"] == "active"

            # Cache reflects new value.
            cached = state.active_agents.get("tok-upd")
            assert cached is not None
            assert cached["status"] == "active"

            # Exactly one event from this update_field call.
            assert len(bus.events) == 1, bus.events
            _addr, event_type, _payload = bus.events[0]
            assert "agent" in event_type
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_update_field_missing_agent_returns_none(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        result = agent_repo.update_field("nope", "status", "active")
        assert result is None


def test_update_field_invalid_field_returns_none(project_dir, reset_globals):
    """The allowlist rejects unknown fields, returning None.

    Mirrors the legacy ``update_agent_db_field`` allowlist guard — the
    class form is a thin shell around it.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-bad", token="tok-bad", status="active")
        # token / agent_id / created_at are NOT in the mutable allowlist.
        assert agent_repo.update_field("agent-bad", "agent_id", "x") is None


# --- Write interface: terminate -----------------------------------------


def test_terminate_success_evicts_caches_and_publishes(
    project_dir, reset_globals,
):
    """``terminate`` is the class form's centralised teardown path.

    Contract:
      1. Sets status='terminated' + bumps terminated_at in the DB.
      2. Evicts BOTH caches (the two-view invariant).
      3. Publishes exactly one ``agent.terminated`` event.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            _seed_agent(
                "agent-term", token="tok-term", status="active",
                working_directory="/tmp/term",
            )
            # Prime the caches.
            agent_repo.get_by_token("tok-term")
            assert "tok-term" in state.active_agents
            bus.events.clear()

            ok = agent_repo.terminate("agent-term")
            assert ok is True

            # Both caches evicted.
            assert "tok-term" not in state.active_agents
            assert "agent-term" not in state.agent_working_dirs

            # DB row still exists but status is terminated.
            row = agent_repo.get_by_id("agent-term")
            assert row is not None
            assert row["status"] == "terminated"

            # Exactly one terminate event.
            term_events = [e for e in bus.events if "terminat" in e[1]]
            assert len(term_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_terminate_missing_returns_false(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        assert agent_repo.terminate("missing") is False
