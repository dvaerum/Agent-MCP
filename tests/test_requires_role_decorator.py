"""Phase 2 Wave 2a — ``@requires_role`` covers manager + operator tiers.

Background
----------

Phase 2 introduces a *manager-agent* tier above worker but below operator
(see plan section 2b in /home/dennis/.claude/plans/prancy-napping-pie.md).

Today ``@requires("admin")`` (alias ``@requires_role("admin")``) is the
only restrictive gate on tool entry points. It accepts the system
bearer (``g.system_token``, formerly ``g.admin_token``) and rejects
everyone else. That conflates "the human operator who logged into the
dashboard" with "an agent spawned with the system token glued onto its
env" — two distinct populations.

This PR introduces two new role names + one alias:

* ``@requires_role("manager")`` — admits ANY of:
  - operator session (the new ContextVar
    :data:`agent_mcp.tools.registry.operator_session_active`, set True
    by REST handlers when the call originates from a logged-in
    operator's session cookie path), OR
  - the system bearer (``g.system_token``), OR
  - an agent token whose row in ``agents`` has
    ``agent_role == 'manager'``.
* ``@requires_role("operator")`` — admits ONLY operator session or
  the system bearer. Agent tokens (worker OR manager) are rejected
  even if they hold the system token by accident (defence in depth).
* ``@requires("admin")`` — kept as a backwards-compat alias for
  ``@requires_role("operator")`` for ONE release. New code uses
  ``"operator"``; the alias quietly forwards.

The tests below pin the matrix:

| Caller                      | manager | operator | any | admin (alias) |
|-----------------------------|---------|----------|-----|---------------|
| operator session (cookie)   | ✅      | ✅       | ✅  | ✅            |
| system bearer (raw)         | ✅      | ✅       | ✅  | ✅            |
| agent_role='manager' token  | ✅      | ❌       | ✅  | ❌            |
| agent_role='worker' token   | ❌      | ❌       | ✅  | ❌            |
| no/garbage token            | ❌      | ❌       | ❌  | ❌            |

The dispatcher integration (operator_session_active ContextVar) is
exercised via the REST seam in ``tests/test_dashboard_session_auth.py``
already; this file pins the decorator's own logic and direct
ContextVar manipulation.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import mcp.types as mcp_types


# --- Plumbing fixtures ------------------------------------------------------


@pytest.fixture
def app_with_db(project_dir, reset_globals, monkeypatch):
    """Bring the in-process app up so the DB schema (including the
    ``agents.agent_role`` column from migration 0013) exists.

    The manager-role checks read from the DB via
    ``agent_repo.get_agent_by_token``; without the schema initialised
    those reads short-circuit to None and the role gate can't be
    exercised against a real manager row.
    """
    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    from agent_mcp.app.main_app import create_app
    from starlette.testclient import TestClient

    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        yield app


def _seed_agent(token: str, agent_id: str, *, agent_role: str) -> None:
    """Insert a row into ``agents`` with the given role + seed the cache.

    The decorator does a cache-first lookup (``agent_repo.get_agent_by_token``)
    so we set both the DB row and the in-memory mirror to avoid
    flakiness on the cache path.
    """
    import datetime as _dt

    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    cur = conn.cursor()
    now = _dt.datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, auto_event_loop, "
        "last_event_seen_at, agent_role) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            agent_id,
            None,
            now,
            "created",
            "/tmp",
            "#aabbcc",
            1,
            None,
            agent_role,
        ),
    )
    conn.commit()
    conn.close()

    # Mirror into the in-memory active_agents cache so the verify_token
    # "agent" branch and get_agent_id paths see this row without a DB
    # round-trip.
    g.active_agents[token] = {
        "agent_id": agent_id,
        "agent_role": agent_role,
        "status": "created",
        "working_directory": "/tmp",
    }


# --- @requires_role("manager") ---------------------------------------------


@pytest.mark.asyncio
async def test_requires_role_manager_admits_operator_session(app_with_db) -> None:
    """An operator-session caller can invoke a manager-gated tool.

    The REST seam sets ``operator_session_active`` to True on the
    ``request_auth_token`` ContextVar's sibling before dispatching.
    """
    from agent_mcp.core.authorize import requires_role
    from agent_mcp.tools.registry import operator_session_active

    @requires_role("manager")
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="manager-ok")]

    token = operator_session_active.set(True)
    try:
        result = await my_tool({})
    finally:
        operator_session_active.reset(token)
    assert result[0].text == "manager-ok"


@pytest.mark.asyncio
async def test_requires_role_manager_admits_manager_agent(app_with_db) -> None:
    """An agent token whose row has agent_role='manager' is admitted."""
    from agent_mcp.core.authorize import requires_role

    _seed_agent("tok-mgr", "mgr_a", agent_role="manager")

    @requires_role("manager")
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="manager-agent-ok")]

    result = await my_tool({"token": "tok-mgr"})
    assert result[0].text == "manager-agent-ok"


@pytest.mark.asyncio
async def test_requires_role_manager_rejects_worker_agent(app_with_db) -> None:
    """Worker-role agent token gets AuthRejected on a manager-gated tool."""
    from agent_mcp.core.authorize import requires_role, AuthRejected

    _seed_agent("tok-wkr", "wkr_a", agent_role="worker")

    @requires_role("manager")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should not run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": "tok-wkr"})


@pytest.mark.asyncio
async def test_requires_role_manager_rejects_system_token(app_with_db) -> None:
    """retire-system-token Wave 1: the system bearer NO LONGER passes
    manager gates. The god-key admit was removed from ``verify_token``;
    the surviving manager-tier admits are (a) operator session and
    (b) a per-agent token whose row has ``agent_role='manager'``."""
    from agent_mcp.core import globals as g
    from agent_mcp.core.authorize import requires_role, AuthRejected

    @requires_role("manager")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should-not-run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": g.system_token})


# --- @requires_role("operator") --------------------------------------------


@pytest.mark.asyncio
async def test_requires_role_operator_admits_operator_session(app_with_db) -> None:
    """Operator session passes the operator gate."""
    from agent_mcp.core.authorize import requires_role
    from agent_mcp.tools.registry import operator_session_active

    @requires_role("operator")
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="op-ok")]

    token = operator_session_active.set(True)
    try:
        result = await my_tool({})
    finally:
        operator_session_active.reset(token)
    assert result[0].text == "op-ok"


@pytest.mark.asyncio
async def test_requires_role_operator_rejects_system_token(app_with_db) -> None:
    """retire-system-token Wave 1: the system bearer NO LONGER passes
    the operator gate. The god-key admit was removed; operator-tier
    callers must prove identity via operator session (cookie or
    signed forwarding header). Tests that previously passed
    ``token=g.system_token`` here must now stamp
    ``operator_session_active`` instead."""
    from agent_mcp.core import globals as g
    from agent_mcp.core.authorize import requires_role, AuthRejected

    @requires_role("operator")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should-not-run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": g.system_token})


@pytest.mark.asyncio
async def test_requires_role_operator_rejects_manager_agent(app_with_db) -> None:
    """A manager-role agent token is rejected by the operator gate.

    This is the load-bearing distinction between manager + operator:
    managers supervise agents; they CANNOT do operator-only actions
    (spawn agents, mutate config_* secrets, broadcast messages).
    """
    from agent_mcp.core.authorize import requires_role, AuthRejected

    _seed_agent("tok-mgr-op", "mgr_op", agent_role="manager")

    @requires_role("operator")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should not run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": "tok-mgr-op"})


@pytest.mark.asyncio
async def test_requires_role_operator_rejects_worker_agent(app_with_db) -> None:
    """Worker agent token is rejected by the operator gate."""
    from agent_mcp.core.authorize import requires_role, AuthRejected

    _seed_agent("tok-wkr-op", "wkr_op", agent_role="worker")

    @requires_role("operator")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should not run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": "tok-wkr-op"})


# --- Backwards-compat: @requires("admin") still works -----------------------


@pytest.mark.asyncio
async def test_legacy_requires_admin_still_works(app_with_db) -> None:
    """``@requires("admin")`` continues to authorise an operator session.

    retire-system-token Wave 1: the legacy alias is preserved, but the
    god-key bearer that previously admitted it is gone. The surviving
    admit path is an operator session (stamped by the REST seam, the
    forwarding-header middleware path, or the test harness).
    """
    from agent_mcp.core.authorize import requires
    from agent_mcp.tools.registry import operator_session_active

    @requires("admin")
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="legacy-ok")]

    cv = operator_session_active.set(True)
    try:
        result = await my_tool({"token": "anything-ignored"})
    finally:
        operator_session_active.reset(cv)
    assert result[0].text == "legacy-ok"


@pytest.mark.asyncio
async def test_legacy_requires_admin_rejects_manager_agent(app_with_db) -> None:
    """``@requires("admin")`` must continue to reject manager agents.

    The legacy alias maps to ``"operator"`` semantics — a manager
    can't perform operator-only actions via the legacy decorator
    either.
    """
    from agent_mcp.core.authorize import requires, AuthRejected

    _seed_agent("tok-mgr-leg", "mgr_leg", agent_role="manager")

    @requires("admin")
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return [mcp_types.TextContent(type="text", text="should not run")]

    with pytest.raises(AuthRejected):
        await my_tool({"token": "tok-mgr-leg"})


# --- @requires_role("any") unchanged ----------------------------------------


@pytest.mark.asyncio
async def test_requires_role_any_admits_worker_token(app_with_db) -> None:
    """``"any"`` still admits any active agent (worker or manager)."""
    from agent_mcp.core.authorize import requires_role

    _seed_agent("tok-any-w", "any_w", agent_role="worker")

    @requires_role("any")
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="any-ok")]

    result = await my_tool({"token": "tok-any-w"})
    assert result[0].text == "any-ok"


# --- access.py classification surfaces manager + operator ------------------


def test_access_derives_manager_tag(app_with_db) -> None:
    """Tools decorated ``@requires_role("manager")`` and registered with
    ``visibility="manager"`` surface as ``"manager"`` in the derived
    classification map.

    The tools/list filter is the consumer; this test pins the
    derivation glue so a future tool with ``@requires_role("manager")``
    automatically appears under the manager bucket without anyone
    hand-maintaining a dict.
    """
    from agent_mcp.core.authorize import requires_role
    from agent_mcp.tools.access import _derive_access_level

    @requires_role("manager")
    async def fake_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return []

    # Synthesize a registry-shaped entry (just the meta surface
    # _derive_access_level reads).
    class _Meta:
        implementation = fake_tool
        declared_visibility = "manager"

    class _Entry:
        name = "fake_manager_tool"
        meta = _Meta()

    assert _derive_access_level(_Entry()) == "manager"


def test_access_derives_operator_tag(app_with_db) -> None:
    """``@requires_role("operator")`` derives to the ``"operator"`` tag.

    The tag is distinct from the legacy ``"admin"`` so the dashboard
    UI can show "operator-only" vs "manager-or-operator" badges
    correctly.
    """
    from agent_mcp.core.authorize import requires_role
    from agent_mcp.tools.access import _derive_access_level

    @requires_role("operator")
    async def fake_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover
        return []

    class _Meta:
        implementation = fake_tool
        declared_visibility = "operator"

    class _Entry:
        name = "fake_operator_tool"
        meta = _Meta()

    assert _derive_access_level(_Entry()) == "operator"


def test_is_visible_to_role_manager_role_sees_manager_tools(app_with_db) -> None:
    """A manager-role caller sees manager-classified tools in tools/list.

    Workers do not; operators do (they see everything).
    """
    from agent_mcp.tools.access import is_visible_to_role, TOOL_ACCESS

    # Inject a synthetic classification so we can test the filter
    # without depending on what's actually decorated yet.
    snapshot = TOOL_ACCESS()
    snapshot["__test_manager_tool__"] = "manager"

    import agent_mcp.tools.access as access_mod

    original = access_mod._build_access_map

    def _fake_build():
        return snapshot

    access_mod._build_access_map = _fake_build
    try:
        assert is_visible_to_role("__test_manager_tool__", "manager") is True
        assert is_visible_to_role("__test_manager_tool__", "worker") is False
        assert is_visible_to_role("__test_manager_tool__", "admin") is True
    finally:
        access_mod._build_access_map = original
