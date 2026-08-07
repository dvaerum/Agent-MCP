"""PR1 unit + integration tests for agent self-service profiles.

Covers (plan §7 PR1):

* ``AgentRepository.review_profile`` — review-vs-change semantics.
* ``update_agent_profile`` tool — the gating matrix.
* manager registration seeds the charter (reviewed = updated = created);
  worker registration leaves the profile NULL.

The get_system_prompt self-read assertions the plan's §7/§8 sketch under
PR1 are DELIBERATELY NOT here: locked design decision 7 moved profile
self-read OFF get_system_prompt onto the PR3 event-loop ``profile_review``
surface ("Nothing goes into get_system_prompt"). PR1's §6 bullet states
"PR1 does not touch get_system_prompt". This file honours the locked
decision; the self-read integration lands in PR3.
"""

from __future__ import annotations

import datetime

from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app
from agent_mcp.core.capabilities import resolve_capabilities
from agent_mcp.core.principal import Principal


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


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
            )
        )
        session.commit()
    return f"tok-{agent_id}"


def _principal(agent_id, role="worker"):
    caps = resolve_capabilities(
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        agent_role=role,
        project_role=None,
        kind="agent_bearer",
    )
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=role,
        can_wake_loop=False,
        source_token=f"tok-{agent_id}",
        capabilities=caps,
    )


def _get_row(agent_id):
    from agent_mcp.repositories import agent_repo

    return agent_repo.get_by_id(agent_id)


def _set_config(key, value):
    """Write a config_* bool into project_settings (JSON-encoded)."""
    import json

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        now = datetime.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO project_settings (context_key, value, updated_at, "
            "updated_by) VALUES (?, ?, ?, 'test') "
            "ON CONFLICT(context_key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value), now),
        )
        conn.commit()
    finally:
        conn.close()


# --- review_profile: review-vs-change -----------------------------------


def test_review_profile_no_arg_bumps_reviewed_only(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("w1", profile="original text")
        before = _get_row("w1")
        assert before["profile_updated_at"] is None

        result = agent_repo.review_profile("w1", new_profile=None, editor_id="w1")
        assert result is not None
        assert result["changed"] is False
        assert result["profile"] == "original text"
        assert result["profile_reviewed_at"] is not None
        # updated_at stays NULL — no content change.
        assert result["profile_updated_at"] is None
        assert result["profile_updated_by"] is None


def test_review_profile_identical_content_bumps_reviewed_only(
    project_dir, reset_globals,
):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("w1", profile="same text")
        result = agent_repo.review_profile(
            "w1", new_profile="same text", editor_id="w1",
        )
        assert result["changed"] is False
        assert result["profile_reviewed_at"] is not None
        assert result["profile_updated_at"] is None


def test_review_profile_changed_content_bumps_updated_and_editor(
    project_dir, reset_globals,
):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("w1", profile="old")
        result = agent_repo.review_profile(
            "w1", new_profile="brand new", editor_id="mgr",
        )
        assert result["changed"] is True
        assert result["profile"] == "brand new"
        assert result["profile_updated_at"] is not None
        assert result["profile_updated_by"] == "mgr"
        assert result["profile_reviewed_at"] is not None


def test_review_profile_unknown_agent_returns_none(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        assert agent_repo.review_profile("ghost", new_profile="x") is None


# --- update_agent_profile tool: gating matrix ---------------------------


import asyncio


def _run(coro):
    """Run ``coro`` on a fresh event loop.

    ``asyncio.get_event_loop()`` raises "no current event loop" once a
    prior test (TestClient lifespan, async test) has closed the main
    thread's loop; a fresh per-call loop is isolation-safe.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(arguments, principal):
    from agent_mcp.tools.agent_profile_tools import (
        update_agent_profile_tool_impl,
    )

    return _run(
        update_agent_profile_tool_impl(arguments, principal=principal)
    )


def test_worker_self_edit_allowed_when_toggle_on(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("w1")
        res = _call({"profile": "I test things."}, _principal("w1", "worker"))
        assert isinstance(res, Ok)
        assert _get_row("w1")["profile"] == "I test things."


def test_worker_self_edit_denied_when_toggle_off(project_dir, reset_globals):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("w1")
        _set_config("config_allow_worker_update_own_profile", False)
        res = _call({"profile": "blocked"}, _principal("w1", "worker"))
        assert isinstance(res, PermissionDenied)
        assert _get_row("w1")["profile"] is None


def test_manager_self_edit_allowed_when_toggle_on(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        res = _call({"profile": "I manage."}, _principal("m1", "manager"))
        assert isinstance(res, Ok)


def test_manager_self_edit_denied_when_toggle_off(project_dir, reset_globals):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        _set_config("config_allow_manager_update_own_profile", False)
        res = _call({"profile": "blocked"}, _principal("m1", "manager"))
        assert isinstance(res, PermissionDenied)


def test_manager_edits_worker_allowed(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        _seed_agent("w1", role="worker")
        res = _call(
            {"agent_id": "w1", "profile": "curated by manager"},
            _principal("m1", "manager"),
        )
        assert isinstance(res, Ok)
        row = _get_row("w1")
        assert row["profile"] == "curated by manager"
        assert row["profile_updated_by"] == "m1"


def test_manager_edits_worker_denied_when_curate_toggle_off(
    project_dir, reset_globals,
):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        _seed_agent("w1", role="worker")
        _set_config("config_allow_manager_curate_profiles", False)
        res = _call(
            {"agent_id": "w1", "profile": "x"}, _principal("m1", "manager"),
        )
        assert isinstance(res, PermissionDenied)


def test_manager_edits_manager_denied(project_dir, reset_globals):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        _seed_agent("m2", role="manager")
        res = _call(
            {"agent_id": "m2", "profile": "x"}, _principal("m1", "manager"),
        )
        assert isinstance(res, PermissionDenied)


def test_worker_edits_other_denied(project_dir, reset_globals):
    from agent_mcp.core.tool_result import PermissionDenied

    with _make_client(project_dir):
        _seed_agent("w1", role="worker")
        _seed_agent("w2", role="worker")
        res = _call(
            {"agent_id": "w2", "profile": "x"}, _principal("w1", "worker"),
        )
        assert isinstance(res, PermissionDenied)


def test_manager_edits_missing_worker_not_found(project_dir, reset_globals):
    from agent_mcp.core.tool_result import NotFound

    with _make_client(project_dir):
        _seed_agent("m1", role="manager")
        res = _call(
            {"agent_id": "ghost", "profile": "x"}, _principal("m1", "manager"),
        )
        assert isinstance(res, NotFound)


def test_worker_review_only_records_review(project_dir, reset_globals):
    from agent_mcp.core.tool_result import Ok

    with _make_client(project_dir):
        _seed_agent("w1", profile="stable")
        res = _call({}, _principal("w1", "worker"))
        assert isinstance(res, Ok)
        assert res.data["changed"] is False
        assert _get_row("w1")["profile_reviewed_at"] is not None
        assert _get_row("w1")["profile_updated_at"] is None


# --- manager registration seeding ---------------------------------------


def _register(client, name, role):
    from agent_mcp.core.principal import Principal
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    op = Principal(
        kind="operator_session",
        user_id="op",
        agent_id=None,
        sysadmin=True,
        project_name="proj",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        capabilities=frozenset({"*"}),
    )
    import os

    os.environ["MCP_PROJECT_DIR"] = str(client.app.state.project_dir) if hasattr(
        client.app.state, "project_dir"
    ) else os.environ.get("MCP_PROJECT_DIR", "")
    return _run(
        register_agent_tool_impl(
            {"name": name, "role": role}, principal=op,
        )
    )


def test_manager_registration_seeds_charter(project_dir, reset_globals):
    import os

    from agent_mcp.core.agent_profile_defaults import MANAGER_DEFAULT_PROFILE
    from agent_mcp.core.tool_result import Ok

    os.environ["MCP_PROJECT_DIR"] = str(project_dir)
    with _make_client(project_dir) as client:
        os.environ["MCP_PROJECT_DIR"] = str(project_dir)
        res = _register(client, "boss", "manager")
        assert isinstance(res, Ok), res
        row = _get_row("boss")
        assert row["profile"] == MANAGER_DEFAULT_PROFILE
        # reviewed == updated == created (not instantly stale).
        assert row["profile_reviewed_at"] is not None
        assert row["profile_updated_at"] == row["profile_reviewed_at"]
        # Seeded, not authored — no editor, so no peer broadcast.
        assert row["profile_updated_by"] is None


def test_worker_registration_leaves_profile_null(project_dir, reset_globals):
    import os

    from agent_mcp.core.tool_result import Ok

    os.environ["MCP_PROJECT_DIR"] = str(project_dir)
    with _make_client(project_dir) as client:
        os.environ["MCP_PROJECT_DIR"] = str(project_dir)
        res = _register(client, "grunt", "worker")
        assert isinstance(res, Ok), res
        row = _get_row("grunt")
        assert row["profile"] is None
        assert row["profile_updated_at"] is None
        assert row["profile_reviewed_at"] is None
