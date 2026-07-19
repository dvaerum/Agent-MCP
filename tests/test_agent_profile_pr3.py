"""PR3 tests: event-loop profile-review surface (greet + overdue).

Covers (plan §7 PR3):

* ``build_profile_review_section`` — greet on first call of a connection
  even when fresh; block when overdue; None when greeted-and-fresh; None
  for non-agent_bearer; a freshly-seeded manager greets with its charter;
  interval 0 disables overdue but not the greet.
* Integration: a new connection's first fetch/wait carries the block; the
  second (still fresh) does not; an overdue agent carries it until
  ``update_agent_profile()`` resets reviewed_at; a reconnect re-greets.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from agent_mcp.app.main_app import create_app
from agent_mcp.core.capabilities import resolve_capabilities
from agent_mcp.core.principal import Principal
from starlette.testclient import TestClient


def _make_client(project_dir):
    return TestClient(create_app(project_dir=str(project_dir)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clear_greet():
    from agent_mcp.core import session_registry

    session_registry._profile_greeted_agents.clear()
    yield
    session_registry._profile_greeted_agents.clear()


def _seed_agent(
    agent_id, *, role="worker", status="active", profile=None,
    profile_reviewed_at=None,
):
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=f"tok-{agent_id}", agent_id=agent_id, capabilities="[]",
                created_at=now, status=status, current_task=None,
                working_directory="/tmp/seed", color="#abc", terminated_at=None,
                updated_at=now, aoe_session_id=None, agent_role=role,
                profile=profile,
                profile_updated_at=now if profile else None,
                profile_reviewed_at=profile_reviewed_at,
            )
        )
        session.commit()
    return f"tok-{agent_id}"


def _principal(agent_id, role="worker"):
    caps = resolve_capabilities(
        user_id=None, agent_id=agent_id, sysadmin=False, agent_role=role,
        project_role=None, kind="agent_bearer",
    )
    return Principal(
        kind="agent_bearer", user_id=None, agent_id=agent_id, sysadmin=False,
        project_name=None, project_role=None, agent_role=role,
        can_wake_loop=False, source_token=f"tok-{agent_id}", capabilities=caps,
    )


def _set_interval(days):
    import json

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        now = datetime.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO project_settings (context_key, value, updated_at, "
            "updated_by) VALUES ('config_profile_review_interval_days', ?, ?, "
            "'test') ON CONFLICT(context_key) DO UPDATE SET value=excluded.value",
            (json.dumps(days), now),
        )
        conn.commit()
    finally:
        conn.close()


def _build(principal):
    from agent_mcp.core.profile_review import build_profile_review_section

    return build_profile_review_section(principal)


_OLD = "2000-01-01T00:00:00"


# --- unit: build_profile_review_section ---------------------------------


def test_first_call_greets_even_when_fresh(project_dir, reset_globals):
    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent("w", profile="fresh", profile_reviewed_at=now)
        section = _build(_principal("w"))
        assert section is not None
        assert section["profile"] == "fresh"
        # Second call in the same connection (still fresh) → no greet.
        assert _build(_principal("w")) is None


def test_overdue_returns_block(project_dir, reset_globals):
    from agent_mcp.core import session_registry

    with _make_client(project_dir):
        _seed_agent("w", profile="stale", profile_reviewed_at=_OLD)
        _set_interval(7)
        # Pretend already greeted so only the overdue path can fire.
        session_registry.mark_profile_greeted("w")
        section = _build(_principal("w"))
        assert section is not None
        assert section["reason"] in ("overdue", "first_connect_overdue")


def test_greeted_and_fresh_returns_none(project_dir, reset_globals):
    from agent_mcp.core import session_registry

    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent("w", profile="fresh", profile_reviewed_at=now)
        _set_interval(7)
        session_registry.mark_profile_greeted("w")
        assert _build(_principal("w")) is None


def test_non_agent_bearer_returns_none(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("w", profile="x")
        op = Principal(
            kind="operator_session", user_id="op", agent_id=None,
            sysadmin=True, project_name="p", project_role="operator",
            agent_role=None, can_wake_loop=False, source_token=None,
            capabilities=frozenset({"*"}),
        )
        assert _build(op) is None


def test_fresh_manager_greets_with_charter(project_dir, reset_globals):
    from agent_mcp.core.agent_profile_defaults import MANAGER_DEFAULT_PROFILE

    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent(
            "m", role="manager", profile=MANAGER_DEFAULT_PROFILE,
            profile_reviewed_at=now,
        )
        section = _build(_principal("m", "manager"))
        assert section is not None
        assert section["profile"] == MANAGER_DEFAULT_PROFILE


def test_interval_zero_disables_overdue_but_not_greet(project_dir, reset_globals):
    from agent_mcp.core import session_registry

    with _make_client(project_dir):
        _seed_agent("w", profile="stale", profile_reviewed_at=_OLD)
        _set_interval(0)
        # First-connect greet still fires despite interval 0.
        assert _build(_principal("w")) is not None
        # After greeting, interval 0 → no overdue re-surface.
        assert session_registry.is_profile_greeted("w")
        assert _build(_principal("w")) is None


def test_blank_worker_greet_prompts_authoring(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("w")  # no profile
        section = _build(_principal("w"))
        assert section is not None
        assert section["profile"] in (None, "")
        assert "update_agent_profile" in section["instruction"]


# --- integration: through the event-loop tools --------------------------


def _fetch(agent_id, role="worker"):
    from agent_mcp.tools.agent_communication_tools import (
        fetch_events_since_tool_impl,
    )

    res = _run(
        fetch_events_since_tool_impl(
            {"cursor": datetime.datetime.now().isoformat()},
            principal=_principal(agent_id, role),
        )
    )
    return res.data


def test_first_fetch_carries_block_second_does_not(project_dir, reset_globals):
    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent("w", profile="fresh", profile_reviewed_at=now)
        first = _fetch("w")
        assert "profile_review" in first
        second = _fetch("w")
        assert "profile_review" not in second


def test_wait_for_events_carries_block(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        wait_for_events_tool_impl,
    )

    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent("w", profile="fresh", profile_reviewed_at=now)
        res = _run(
            wait_for_events_tool_impl(
                {"since": now, "timeout_seconds": 1},
                principal=_principal("w"),
            )
        )
        assert "profile_review" in res.data


def test_overdue_persists_until_review_resets(project_dir, reset_globals):
    from agent_mcp.core import session_registry
    from agent_mcp.tools.agent_profile_tools import (
        update_agent_profile_tool_impl,
    )

    with _make_client(project_dir):
        _seed_agent("w", profile="stale", profile_reviewed_at=_OLD)
        _set_interval(7)
        # First fetch greets (and is overdue).
        assert "profile_review" in _fetch("w")
        # Still overdue on the next call (greeted, but overdue keeps firing).
        assert "profile_review" in _fetch("w")
        # Confirm the review (no content change) → reviewed_at = now.
        _run(
            update_agent_profile_tool_impl({}, principal=_principal("w"))
        )
        # Not overdue any more, and already greeted → block gone.
        assert "profile_review" not in _fetch("w")
        session_registry._profile_greeted_agents  # noqa: B018 - keep import used


def test_reconnect_re_greets(project_dir, reset_globals):
    from agent_mcp.core import session_registry

    with _make_client(project_dir):
        now = datetime.datetime.now().isoformat()
        _seed_agent("w", profile="fresh", profile_reviewed_at=now)
        assert "profile_review" in _fetch("w")
        assert "profile_review" not in _fetch("w")
        # Simulate a reconnect: opening a GET /mcp stream resets the greet.
        sid = session_registry.register_session(
            agent_id="w", bearer_token="tok-w",
        )
        assert not session_registry.is_profile_greeted("w")
        assert "profile_review" in _fetch("w")
        session_registry.unregister_session(sid)
