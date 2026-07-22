"""tools/list filtering by bearer role.

Workers must NOT see admin-only tools in their MCP `tools/list` catalogue.
Phase 7f deleted the router-side rewrite (per Q7.1: "no MCP-protocol
manipulation in the router"), so the backend itself must filter.

Per-tool access classification (see `agent_mcp.tools.access`):

  * "admin"  → admin sees, worker does not, unauthenticated does not.
  * "any"    → everyone sees (admin/worker/unauthenticated alike).
  * "worker-if-toggled:<key>" → worker sees iff the project_context key
    resolves truthy (with the toggle's own default); admin always sees.

These tests pin the filter against the full registered catalogue so a
new admin-gated tool registered without an access classification is
caught (defaults to "any" today, but the test makes the policy explicit
per tool).

Migrated to use `tests/harness.py::mcp_session` (Candidate E from
architecture review 2026-06-01). The `_list_tools_via_framework`
helper and the ContextVar-juggling around `request_auth_token`
collapse into `session.list_tools()`. The structural classification
test that doesn't need a running app stays as a plain function.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


# Note: tests in this module mix one sync structural test
# (`test_every_registered_tool_has_access_classification`) with async
# harness-driven tests. We mark the async ones individually rather
# than at module level so pytest-asyncio doesn't warn about the sync
# function carrying an asyncio mark.


def _names(tools) -> set[str]:
    return {t.name for t in tools}


async def _promote_to_manager(admin, session):
    """Promote a freshly-created worker session to manager role.

    The harness has no `create_manager`; mirror the documented pattern
    (see tests/test_scheduled_directive_tools.py) — flip the DB row AND
    the in-memory cache the session's Principal reads. Returns `session`
    so callers can chain.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.cursor().execute(
            "UPDATE agents SET agent_role='manager' WHERE agent_id=?",
            (session.agent_id,),
        )
        conn.commit()
    finally:
        conn.close()
    if session.token in g.active_agents:
        g.active_agents[session.token]["agent_role"] = "manager"
    return session


# --- Access classification: the registry must classify every tool ---


def test_every_registered_tool_has_access_classification() -> None:
    """Every name in `tool_schemas` must have an entry in
    `TOOL_ACCESS` (i.e. an explicit admin/any/worker-if-toggled
    decision). New tools without a classification would default to
    "any", which is the most permissive — making the classification
    mandatory catches that omission.

    Pure structural assertion: no app, no harness needed.
    """
    import agent_mcp.tools  # noqa: F401 — registers tools
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import tool_schemas

    registered = {e["name"] for e in tool_schemas}
    classified = set(TOOL_ACCESS.keys())
    missing = sorted(registered - classified)
    assert not missing, (
        "Tools registered without an access classification (add to "
        f"agent_mcp/tools/access.py::TOOL_ACCESS): {missing}"
    )


# --- Filter behavior: admin sees everything, worker sees subset ---

# Wave 7 PR 3 (coordinator transition): ``create_agent`` (spawn) and
# ``relaunch_agent`` (tmux send-keys to an existing session) are
# gone. ``register_agent`` is the sole agent-creation surface; relaunch
# has no analogue under the coordinator model (the user starts and
# stops their own claude session).
ADMIN_ONLY = {
    "register_agent",
    "view_status",
    "terminate_agent",
    "view_audit_log",
    "get_agent_tokens",
    "broadcast_admin_message",
    "bulk_task_operations",
    "delete_task",
    "backup_project_context",
}


@pytest.mark.asyncio
async def test_admin_bearer_sees_every_registered_tool(tmp_path) -> None:
    """Admin role → no filter; tools/list returns the full catalogue."""
    from agent_mcp.tools.registry import tool_schemas

    async with mcp_session(tmp_path) as admin:
        tools = await admin.list_tools()
        names = _names(tools)
        registered = {e["name"] for e in tool_schemas}
        assert names == registered, (
            f"admin should see every registered tool; missing="
            f"{sorted(registered - names)}, extra={sorted(names - registered)}"
        )


@pytest.mark.asyncio
async def test_worker_bearer_does_not_see_admin_only_tools(tmp_path) -> None:
    """Worker role → all "admin"-classified tools are filtered out."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-filter")
        tools = await alice.list_tools()
        names = _names(tools)
        leaked = names & ADMIN_ONLY
        assert not leaked, (
            f"worker tools/list leaked admin-only tools: {sorted(leaked)}"
        )


@pytest.mark.asyncio
async def test_worker_bearer_sees_any_tools(tmp_path) -> None:
    """Worker role → "any"-classified tools remain visible."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-any")
        tools = await alice.list_tools()
        names = _names(tools)
        expected_visible = {
            "view_project_context",
            "view_tasks",
            "view_file_metadata",
            "get_agent_messages",
            "request_assistance",
            "ask_project_rag",
            "test",
            "get_system_prompt",
        }
        missing = expected_visible - names
        assert not missing, (
            f"worker tools/list dropped expected 'any' tools: "
            f"{sorted(missing)}"
        )


@pytest.mark.asyncio
async def test_worker_send_agent_message_hidden_when_toggle_off(
    tmp_path,
) -> None:
    """`send_agent_message` is gated on `config_allow_worker_to_worker`.
    With the toggle EXPLICITLY off, a worker should NOT see it (the
    explicit-off path — the default is now True).
    """
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_to_worker", "false")
        alice = await admin.create_worker("alice-w2w")
        tools = await alice.list_tools()
        assert "send_agent_message" not in _names(tools), (
            "worker should not see send_agent_message when "
            "config_allow_worker_to_worker is explicitly false"
        )


@pytest.mark.asyncio
async def test_worker_send_agent_message_visible_by_default(tmp_path) -> None:
    """`config_allow_worker_to_worker` now defaults to True — a worker
    with no toggle set DOES see `send_agent_message`.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-w2w-default")
        tools = await alice.list_tools()
        assert "send_agent_message" in _names(tools), (
            "worker should see send_agent_message by default "
            "(config_allow_worker_to_worker defaults to True)"
        )


@pytest.mark.asyncio
async def test_manager_send_agent_message_visible_by_default(tmp_path) -> None:
    """A manager-role agent also sees `send_agent_message` by default —
    the gate treats managers like workers, and the default is now True.
    """
    async with mcp_session(tmp_path) as admin:
        bossy = await _promote_to_manager(
            admin, await admin.create_worker("bossy-mgr")
        )
        tools = await bossy.list_tools()
        assert "send_agent_message" in _names(tools), (
            "manager should see send_agent_message by default "
            "(config_allow_worker_to_worker defaults to True)"
        )


@pytest.mark.asyncio
async def test_worker_send_agent_message_visible_when_toggle_on(
    tmp_path,
) -> None:
    """Flip `config_allow_worker_to_worker=true` and the tool appears."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_to_worker", "true")
        alice = await admin.create_worker("alice-w2w-on")
        tools = await alice.list_tools()
        assert "send_agent_message" in _names(tools), (
            "worker should see send_agent_message when "
            "config_allow_worker_to_worker=true"
        )


@pytest.mark.asyncio
async def test_worker_update_task_status_visible_by_default(tmp_path) -> None:
    """`update_task_status` is toggle-gated but defaults to True
    (PR #18) — worker should see it without any explicit toggle.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-uts")
        tools = await alice.list_tools()
        assert "update_task_status" in _names(tools)


@pytest.mark.asyncio
async def test_worker_update_task_status_hidden_when_toggle_off(
    tmp_path,
) -> None:
    """Flip `config_allow_worker_update_own_status=false` → hidden."""
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle(
            "config_allow_worker_update_own_status", "false"
        )
        alice = await admin.create_worker("alice-uts-off")
        tools = await alice.list_tools()
        assert "update_task_status" not in _names(tools)


@pytest.mark.asyncio
async def test_worker_assign_task_visible_by_default(tmp_path) -> None:
    """`assign_task` defaults — both `self_assign` and
    `create_unassigned` default True. With either toggle truthy the
    tool is visible to workers.
    """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-assign")
        tools = await alice.list_tools()
        assert "assign_task" in _names(tools)


@pytest.mark.asyncio
async def test_worker_assign_task_hidden_when_both_toggles_off(
    tmp_path,
) -> None:
    """When BOTH `config_allow_worker_self_assign` and
    `config_allow_worker_create_unassigned` are false, the worker
    can't usefully call `assign_task` — hide it.
    """
    async with mcp_session(tmp_path) as admin:
        admin.set_toggle("config_allow_worker_self_assign", "false")
        admin.set_toggle(
            "config_allow_worker_create_unassigned", "false"
        )
        alice = await admin.create_worker("alice-assign-off")
        tools = await alice.list_tools()
        assert "assign_task" not in _names(tools)


@pytest.mark.asyncio
async def test_unauthenticated_sees_only_any_tools(tmp_path) -> None:
    """No bearer set → conservative: show only "any" tools."""
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import request_auth_token

    async with mcp_session(tmp_path) as admin:
        # The harness's WorkerSession always binds *some* bearer in
        # request_auth_token. To exercise the "unauthenticated" path
        # we explicitly clear the contextvar around list_tools by
        # going through the framework handler with a None token
        # bound. The admin.list_tools() flow would bind admin_token,
        # so we drive the handler directly here using admin's lazy
        # accessor.
        import mcp.types as mcp_types

        handler = admin._list_tools_handler()
        req = mcp_types.ListToolsRequest(method="tools/list")
        cv_token = request_auth_token.set(None)
        try:
            result = await handler(req)
        finally:
            request_auth_token.reset(cv_token)
        inner = result.root if hasattr(result, "root") else result
        tools = list(getattr(inner, "tools", []) or [])

        names = _names(tools)
        expected_any = {n for n, lvl in TOOL_ACCESS.items() if lvl == "any"}
        assert names == expected_any, (
            f"unauthenticated should see exactly 'any' tools; "
            f"missing={sorted(expected_any - names)}, "
            f"extra={sorted(names - expected_any)}"
        )
