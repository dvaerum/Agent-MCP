"""PR4 tests: `view_agents` peer roster tool.

Covers (plan §7 PR4):

* Unit: lists active agents with profiles; excludes tombstone/terminated
  /system; no token/secret fields leak.
* Integration: a worker bearer can call view_agents and see a peer's
  profile.
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


def _seed_agent(agent_id, *, role="worker", status="active", profile=None):
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
                profile_updated_at=now if profile else None,
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


def _anon_principal():
    return Principal(
        kind="agent_bearer", user_id=None, agent_id=None, sysadmin=False,
        project_name=None, project_role=None, agent_role=None,
        can_wake_loop=False, source_token=None, capabilities=frozenset(),
    )


def _call(principal):
    from agent_mcp.tools.agent_roster_tools import view_agents_tool_impl

    return _run(view_agents_tool_impl({}, principal=principal))


def test_view_agents_lists_active_with_profiles(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("alice", profile="I do iOS.")
        _seed_agent("bob", role="manager", profile="I coordinate.")
        res = _call(_principal("alice", "worker"))
        assert isinstance(res, Ok)
        agents = {a["agent_id"]: a for a in res.data["agents"]}
        assert set(agents) == {"alice", "bob"}
        assert agents["alice"]["profile"] == "I do iOS."
        assert agents["bob"]["agent_role"] == "manager"
        assert agents["bob"]["profile"] == "I coordinate."


def test_view_agents_excludes_terminated_tombstone_system(
    project_dir, reset_globals,
):
    with _make_client(project_dir):
        _seed_agent("live1")
        _seed_agent("dead", status="terminated", profile="gone")
        _seed_agent("ghost", status="tombstone", profile="x")
        _seed_agent("sys", status="system", profile="y")
        res = _call(_principal("live1", "worker"))
        ids = {a["agent_id"] for a in res.data["agents"]}
        assert ids == {"live1"}


def test_view_agents_leaks_no_token_or_secret(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("alice", profile="hi")
        res = _call(_principal("alice", "worker"))
        for a in res.data["agents"]:
            assert set(a.keys()) == {
                "agent_id", "agent_role", "profile", "profile_updated_at",
            }
            assert "token" not in a
            assert "working_directory" not in a


def test_view_agents_worker_sees_peer_profile(project_dir, reset_globals):
    with _make_client(project_dir):
        _seed_agent("alice", profile="alice prose")
        _seed_agent("bob", profile="bob prose")
        res = _call(_principal("alice", "worker"))
        bob = next(a for a in res.data["agents"] if a["agent_id"] == "bob")
        assert bob["profile"] == "bob prose"


def test_view_agents_denies_anonymous(project_dir, reset_globals):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("alice")
        res = _call(_anon_principal())
        assert isinstance(res, PermissionDenied)


def test_view_agents_admits_operator(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("alice", profile="hi")
        op = Principal(
            kind="operator_session", user_id="op", agent_id=None,
            sysadmin=False, project_name="proj", project_role="operator",
            agent_role=None, can_wake_loop=False, source_token=None,
            capabilities=resolve_capabilities(
                user_id="op", agent_id=None, sysadmin=False, agent_role=None,
                project_role="operator", kind="operator_session",
            ),
        )
        res = _call(op)
        assert isinstance(res, Ok)
        assert any(a["agent_id"] == "alice" for a in res.data["agents"])
