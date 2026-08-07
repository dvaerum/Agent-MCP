"""PR2 tests: agent_profile_updated peer-broadcast event flow.

Covers (plan §7 PR2):

* ``_collect_agent_profile_events_for`` — changed-since rows, excludes
  self, excludes rows the recipient authored (updated_by == me), excludes
  tombstone/terminated.
* Integration: A self-edits → B's ``fetch_events_since`` yields
  ``agent_profile_updated``; A's own does not. Manager M edits worker W →
  W receives it (editor exclusion, not subject). Disconnect/catch-up: an
  old cursor replays the event (the agents table is the log).
"""

from __future__ import annotations

import asyncio
import datetime

from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app
from agent_mcp.core.capabilities import resolve_capabilities
from agent_mcp.core.principal import Principal


def _make_client(project_dir):
    return TestClient(create_app(project_dir=str(project_dir)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed_agent(
    agent_id,
    *,
    role="worker",
    status="active",
    profile=None,
    profile_updated_at=None,
    profile_updated_by=None,
):
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=f"tok-{agent_id}",
                agent_id=agent_id,
                created_at=now,
                status=status,
                current_task=None,
                working_directory="/tmp/seed",
                color="#abcdef",
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
                agent_role=role,
                profile=profile,
                profile_updated_at=profile_updated_at,
                profile_reviewed_at=profile_updated_at,
                profile_updated_by=profile_updated_by,
            )
        )
        session.commit()
    return f"tok-{agent_id}"


def _principal(agent_id, role="worker"):
    caps = resolve_capabilities(
        user_id=None, agent_id=agent_id, sysadmin=False,
        agent_role=role, project_role=None, kind="agent_bearer",
    )
    return Principal(
        kind="agent_bearer", user_id=None, agent_id=agent_id, sysadmin=False,
        project_name=None, project_role=None, agent_role=role,
        can_wake_loop=False, source_token=f"tok-{agent_id}", capabilities=caps,
    )


_TS = "2026-07-19T12:00:00"


# --- collector unit tests -----------------------------------------------


def test_collector_returns_changed_since_rows(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        _collect_agent_profile_events_for,
    )

    with _make_client(project_dir):
        _seed_agent("me")
        _seed_agent(
            "peer", profile="peer prose",
            profile_updated_at=_TS, profile_updated_by="peer",
        )
        events = _collect_agent_profile_events_for(
            "me", "2026-07-19T00:00:00",
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "agent_profile_updated"
        assert ev["ref_id"] == "peer"
        assert ev["timestamp"] == _TS
        assert ev["data"]["profile"] == "peer prose"
        assert ev["data"]["updated_by"] == "peer"


def test_collector_excludes_self_as_subject(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        _collect_agent_profile_events_for,
    )

    with _make_client(project_dir):
        _seed_agent(
            "me", profile="mine", profile_updated_at=_TS,
            profile_updated_by="me",
        )
        events = _collect_agent_profile_events_for("me", "2026-07-19T00:00:00")
        assert events == []


def test_collector_excludes_rows_i_authored(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        _collect_agent_profile_events_for,
    )

    with _make_client(project_dir):
        _seed_agent("me", role="manager")
        # Manager "me" curated worker "w" — recipient "me" is the editor,
        # so it must NOT see its own curation echoed back.
        _seed_agent(
            "w", profile="curated", profile_updated_at=_TS,
            profile_updated_by="me",
        )
        events = _collect_agent_profile_events_for("me", "2026-07-19T00:00:00")
        assert events == []


def test_collector_excludes_terminated_and_tombstone(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        _collect_agent_profile_events_for,
    )

    with _make_client(project_dir):
        _seed_agent("me")
        _seed_agent(
            "gone", status="terminated", profile="x",
            profile_updated_at=_TS, profile_updated_by="gone",
        )
        _seed_agent(
            "ghost", status="tombstone", profile="y",
            profile_updated_at=_TS, profile_updated_by="ghost",
        )
        events = _collect_agent_profile_events_for("me", "2026-07-19T00:00:00")
        assert events == []


def test_collector_respects_cursor(project_dir, reset_globals):
    from agent_mcp.tools.agent_communication_tools import (
        _collect_agent_profile_events_for,
    )

    with _make_client(project_dir):
        _seed_agent("me")
        _seed_agent(
            "peer", profile="p", profile_updated_at="2026-07-19T09:00:00",
            profile_updated_by="peer",
        )
        # Cursor after the update → nothing new.
        assert _collect_agent_profile_events_for(
            "me", "2026-07-19T10:00:00",
        ) == []
        # Cursor before → the event.
        assert len(
            _collect_agent_profile_events_for("me", "2026-07-19T08:00:00")
        ) == 1


# --- integration: real tool → feed flow ---------------------------------


def _fetch(agent_id, role, cursor):
    from agent_mcp.tools.agent_communication_tools import (
        fetch_events_since_tool_impl,
    )

    res = _run(
        fetch_events_since_tool_impl(
            {"cursor": cursor}, principal=_principal(agent_id, role),
        )
    )
    return res.data["events"]


def _update(caller, role, arguments):
    from agent_mcp.tools.agent_profile_tools import (
        update_agent_profile_tool_impl,
    )

    return _run(
        update_agent_profile_tool_impl(arguments, principal=_principal(caller, role))
    )


def test_self_edit_reaches_peer_not_self(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("a")
        _seed_agent("b")
        before = datetime.datetime.now().isoformat()
        res = _update("a", "worker", {"profile": "I am agent A."})
        assert res.data["changed"] is True

        b_events = _fetch("b", "worker", before)
        assert any(
            e["type"] == "agent_profile_updated"
            and e["data"]["agent_id"] == "a"
            for e in b_events
        ), b_events

        a_events = _fetch("a", "worker", before)
        assert not any(
            e["type"] == "agent_profile_updated" for e in a_events
        ), a_events


def test_manager_edit_of_worker_reaches_worker(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("m", role="manager")
        _seed_agent("w", role="worker")
        before = datetime.datetime.now().isoformat()
        res = _update(
            "m", "manager", {"agent_id": "w", "profile": "curated by manager"},
        )
        assert res.data["changed"] is True

        # Worker W (the subject) IS notified — editor exclusion, not
        # subject exclusion.
        w_events = _fetch("w", "worker", before)
        assert any(
            e["type"] == "agent_profile_updated"
            and e["data"]["agent_id"] == "w"
            and e["data"]["updated_by"] == "m"
            for e in w_events
        ), w_events

        # Manager M (the editor) does NOT get its own edit echoed.
        m_events = _fetch("m", "manager", before)
        assert not any(
            e["type"] == "agent_profile_updated" for e in m_events
        ), m_events


def test_no_op_review_produces_no_broadcast(project_dir, reset_globals):
    with _make_client(project_dir):
        # Seed a's existing update in the PAST so B's cursor (now) is
        # already ahead of it — only a NEW change would surface.
        _seed_agent("a", profile="stable",
                    profile_updated_at="2026-07-19T00:00:00",
                    profile_updated_by="a")
        _seed_agent("b")
        before = datetime.datetime.now().isoformat()
        # Review-only confirm (no profile arg) — reviewed_at moves, no
        # content change, so no peer broadcast.
        res = _update("a", "worker", {})
        assert res.data["changed"] is False

        b_events = _fetch("b", "worker", before)
        assert not any(
            e["type"] == "agent_profile_updated" for e in b_events
        ), b_events


def test_disconnected_peer_replays_on_catch_up(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("a")
        _seed_agent("b")
        # B never had a live waiter — the in-memory push is dropped on the
        # floor. Catch-up from an old cursor must still replay the edit
        # (the agents table is the log).
        old_cursor = "2026-07-19T00:00:00"
        _update("a", "worker", {"profile": "authored while B offline"})
        b_events = _fetch("b", "worker", old_cursor)
        assert any(
            e["type"] == "agent_profile_updated"
            and e["data"]["agent_id"] == "a"
            for e in b_events
        ), b_events
