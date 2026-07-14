"""Phase 2 Wave 3 — per-tool permission matrix end-to-end.

The plan's section 2c (`/home/dennis/.claude/plans/prancy-napping-pie.md`)
defines the tier matrix:

| Action                                  | worker | manager | operator |
|-----------------------------------------|--------|---------|----------|
| Spawn agent (create_agent)              |   ❌   |   ❌    |    ✅    |
| Terminate / restore / purge agent       |   ❌   |   ❌    |    ✅    |
| Assign task to other agent              | toggle |   ✅    |    ✅    |
| Update other agent's task               |   ❌   |   ✅    |    ✅    |
| Read/write non-config_* project context |   ✅   |   ✅    |    ✅    |
| Read/write config_* (secrets, policy)   |   ❌   |   ❌    |    ✅    |
| Backup project context                  |   ❌   |   ❌    |    ✅    |
| broadcast_admin_message                 |   ❌   |   ❌    |    ✅    |
| RAG query                               |   ✅   |   ✅    |    ✅    |

This test file pins the matrix against the live tool entry points
(not the decorator in isolation — that's
``test_requires_role_decorator.py``). Each row drives one or more
``tool_name(arguments)`` calls through the MCP dispatcher and asserts
on the wire response: ``Unauthorized: ...`` for the rejected cells,
non-error TextContent for the admitted cells.

Wave 3's role for these tests:
  * RED — the matrix isn't fully wired today: managers cannot yet
    assign tasks to other agents (they get caught by the worker
    "self-only" guard in ``_authorize_assign_task``), and the
    ``@requires("admin")`` decorator name doesn't yet read as the
    new "operator" vocabulary in the test surface.
  * GREEN — Wave 3 ships ``@requires_role("operator")`` on the
    operator-tier tools and a manager branch in
    ``_authorize_assign_task`` so managers pass the assign-task gate.

The system-bearer (``g.system_token``) is used to simulate the
operator session here. The dispatcher's
``operator_session_active`` ContextVar branch is exercised by the
REST seam tests (``test_dashboard_session_auth.py``); this file
runs through the tool entry points directly to keep the matrix
testable without a logged-in browser.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _set_agent_role(token: str, agent_role: str) -> None:
    """Update an existing agent row's ``agent_role`` + sync the cache.

    The harness's ``create_worker`` lands rows with the default
    ``agent_role='worker'``; this helper flips an existing row to
    ``'manager'`` so the same WorkerSession can exercise the
    manager-tier matrix without a second insert path.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET agent_role = ? WHERE token = ?",
            (agent_role, token),
        )
        conn.commit()
    finally:
        conn.close()
    # Mirror into active_agents so the role-check cache path sees it.
    if token in g.active_agents:
        g.active_agents[token]["agent_role"] = agent_role


def _seed_context_key(key: str, value: str) -> None:
    """Seed a project_context row via raw SQL.

    Bypasses the tool boundary so we can set up state for the
    update/delete matrix without entangling the SUT in the fixture.
    """
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = _dt.datetime.now().isoformat()
        cur.execute(
            "INSERT OR REPLACE INTO project_context "
            "(context_key, value, last_updated, updated_by, "
            "description, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, value, now, "admin", "test", "admin"),
        )
        conn.commit()
    finally:
        conn.close()


# --- create_agent: operator-only -------------------------------------------


async def test_create_agent_rejects_worker(tmp_path) -> None:
    """Worker token calling ``register_agent`` → Unauthorized.

    Wave 7 PR 1 (coordinator transition): the permission-matrix row
    that pinned ``create_agent`` (the spawn tool that orphan-stormed
    claude processes) is preserved verbatim on ``register_agent`` —
    the spawnless sibling shipped in PR 0. Both tools are
    operator-tier; the worker gate is identical.
    """
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-register")
        await wkr.assert_unauthorized(
            "register_agent",
            {"agent_id": "should-not-be-created"},
        )


async def test_create_agent_rejects_manager(tmp_path) -> None:
    """Manager-role token calling ``register_agent`` → Unauthorized.

    Operator-tier tools (register/terminate/etc.) must reject managers;
    that is the load-bearing distinction between manager + operator.

    Wave 7 PR 1: retargeted from ``create_agent`` to ``register_agent``
    for the same reason as the worker test above — same operator-tier
    gate, no tmux spawn.
    """
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-register")
        _set_agent_role(mgr.token, "manager")
        await mgr.assert_unauthorized(
            "register_agent",
            {"agent_id": "should-not-be-created"},
        )


async def test_create_agent_admits_operator(tmp_path) -> None:
    """System bearer (operator equivalent) is NOT rejected by the auth gate.

    Wave 7 PR 1: retargeted from ``create_agent`` (spawn path) to
    ``register_agent`` (register-only). The legacy assertion noted "the
    tool may still fail downstream (e.g. tmux not available in CI)";
    that caveat is gone with register — it just mints a DB row + token
    and returns Ok. We assert on the auth-success shape (no
    Unauthorized prefix) — same matrix row, same auth contract.
    """
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "register_agent",
            {"agent_id": "registered-by-operator"},
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"register_agent must not be Unauthorized for operator; got {text!r}"
        )


# --- terminate_agent: operator-only ----------------------------------------


async def test_terminate_agent_rejects_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-term")
        await wkr.assert_unauthorized(
            "terminate_agent",
            {"agent_id": "wkr-term"},
        )


async def test_terminate_agent_rejects_manager(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-term")
        _set_agent_role(mgr.token, "manager")
        await mgr.assert_unauthorized(
            "terminate_agent",
            {"agent_id": "mgr-term"},
        )


# --- assign_task to other agent --------------------------------------------


async def test_assign_task_rejects_worker_targeting_other(tmp_path) -> None:
    """Worker assigning to another agent → Unauthorized.

    The toggle gates a worker's self-claim / file-unassigned flows;
    targeting another agent is admin-only (and Wave 3 adds manager).
    """
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-assign")
        other = await admin.create_worker("other")
        await wkr.assert_unauthorized(
            "assign_task",
            {
                "agent_token": other.token,
                "task_title": "Should not be created",
                "task_description": "Worker → other-worker assign",
            },
        )


async def test_assign_task_admits_manager_targeting_other(tmp_path) -> None:
    """Manager assigning to another agent → succeeds.

    Wave 3 adds a manager branch in ``_authorize_assign_task`` so the
    matrix is reachable. Before Wave 3 this test fails (managers fall
    through to the worker "self-only" guard which returns an
    Unauthorized text payload).
    """
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-assign")
        _set_agent_role(mgr.token, "manager")
        other = await admin.create_worker("assignee")
        result = await mgr.call(
            "assign_task",
            {
                "agent_token": other.token,
                "task_title": "Manager-delegated task",
                "task_description": "Confirm managers can assign",
            },
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"manager assign_task must not be Unauthorized; got {text!r}"
        )
        assert not getattr(mgr, "_last_is_error", False), (
            f"manager assign_task must succeed; got {result!r}"
        )


# --- broadcast_admin_message: operator-only --------------------------------


async def test_broadcast_admin_message_rejects_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-broadcast")
        await wkr.assert_unauthorized(
            "broadcast_admin_message",
            {"message": "should not send"},
        )


async def test_broadcast_admin_message_rejects_manager(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-broadcast")
        _set_agent_role(mgr.token, "manager")
        await mgr.assert_unauthorized(
            "broadcast_admin_message",
            {"message": "should not send"},
        )


async def test_broadcast_admin_message_admits_operator(tmp_path) -> None:
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        # Need at least one active agent so the broadcast has a
        # recipient set; the auth gate runs first regardless.
        await admin.create_worker("recipient")
        result = await admin.call(
            "broadcast_admin_message",
            {"message": "operator hello"},
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"broadcast must not be Unauthorized for operator; got {text!r}"
        )


# --- backup_project_context: operator-only ---------------------------------


async def test_backup_project_context_rejects_worker(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-backup")
        await wkr.assert_unauthorized("backup_project_context", {})


async def test_backup_project_context_rejects_manager(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-backup")
        _set_agent_role(mgr.token, "manager")
        await mgr.assert_unauthorized("backup_project_context", {})


# --- project_context non-config_* keys: all tiers --------------------------


async def test_update_project_context_worker_regular_key(tmp_path) -> None:
    """Worker writes a non-``config_*`` key → not Unauthorized."""
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-ctx")
        result = await wkr.call(
            "update_project_context",
            {
                "context_key": "wkr_note",
                "context_value": "worker can write regular keys",
            },
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"worker writes to regular keys must not be Unauthorized; "
            f"got {text!r}"
        )


async def test_update_project_context_manager_regular_key(tmp_path) -> None:
    """Manager writes a non-``config_*`` key → not Unauthorized."""
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-ctx")
        _set_agent_role(mgr.token, "manager")
        result = await mgr.call(
            "update_project_context",
            {
                "context_key": "mgr_note",
                "context_value": "manager can write regular keys",
            },
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"manager writes to regular keys must not be Unauthorized; "
            f"got {text!r}"
        )


# --- project_context config_* keys: operator-only --------------------------


async def test_update_project_context_worker_config_key_rejected(
    tmp_path,
) -> None:
    """Worker writing ``config_*`` → Unauthorized.

    Already enforced by ``_check_write_authorization`` before Wave 3;
    pinned here so the matrix is end-to-end visible in one file.
    """
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-cfg")
        await wkr.assert_unauthorized(
            "update_project_context",
            {
                "context_key": "config_secret",
                "context_value": "should not land",
            },
        )


async def test_update_project_context_manager_config_key_rejected(
    tmp_path,
) -> None:
    """Manager writing ``config_*`` → Unauthorized.

    Managers are NOT operators; the matrix puts ``config_*`` mutation
    in the operator-only column. The existing ``_check_write_authorization``
    treats anything that isn't ``verify_token(token, "admin")`` as a
    worker → that already rejects managers; this test pins the contract.
    """
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-cfg")
        _set_agent_role(mgr.token, "manager")
        await mgr.assert_unauthorized(
            "update_project_context",
            {
                "context_key": "config_secret_mgr",
                "context_value": "should not land",
            },
        )


async def test_update_project_settings_operator_config_key_admitted(
    tmp_path,
) -> None:
    """Operator writes ``config_*`` → not Unauthorized (ADR-0016: on the
    settings store; the context path rejects the namespace outright)."""
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "update_project_settings",
            {
                "context_key": "config_operator_only",
                "context_value": "operator can land config_* keys",
                "description": "matrix test",
            },
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"operator writes to config_* must not be Unauthorized; "
            f"got {text!r}"
        )


# --- RAG query: all tiers --------------------------------------------------


async def test_ask_project_rag_admits_worker(tmp_path) -> None:
    """Worker can call ``ask_project_rag`` (not Unauthorized)."""
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-rag")
        result = await wkr.call(
            "ask_project_rag",
            {"query": "any question"},
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"worker RAG query must not be Unauthorized; got {text!r}"
        )


async def test_ask_project_rag_admits_manager(tmp_path) -> None:
    from tests.harness import _first_text, _is_unauthorized

    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr-rag")
        _set_agent_role(mgr.token, "manager")
        result = await mgr.call(
            "ask_project_rag",
            {"query": "any question"},
        )
        text = _first_text(result)
        assert not _is_unauthorized(text), (
            f"manager RAG query must not be Unauthorized; got {text!r}"
        )
