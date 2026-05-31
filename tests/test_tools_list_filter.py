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
"""
from __future__ import annotations

import asyncio
import secrets

import mcp.types as mcp_types
import pytest


def _admin_token(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def _seed_worker(name: str = "alice") -> tuple[str, str]:
    """Register a worker; returns (token, agent_id)."""
    import datetime as _dt
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    worker_token = secrets.token_hex(16)
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agents (token, agent_id, capabilities, created_at, "
        "status, working_directory, color, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (worker_token, name, "[]", now, "active", "/tmp", "#888", now),
    )
    conn.commit()
    conn.close()
    g.active_agents[worker_token] = {
        "agent_id": name,
        "status": "active",
        "created_at": now,
        "capabilities": [],
    }
    return worker_token, name


def _set_toggle(client, key: str, value: str, admin_token: str) -> None:
    """Seed/update a project_context toggle via the REST API."""
    r = client.post(
        "/api/memories",
        json={"token": admin_token, "context_key": key, "context_value": value},
    )
    if r.status_code == 409:
        r = client.request(
            "PUT",
            f"/api/memories/{key}",
            json={"token": admin_token, "context_value": value},
        )
    assert r.status_code == 200, r.text


async def _list_tools_via_framework() -> list[mcp_types.Tool]:
    """Drive the MCP server's registered ListToolsRequest handler.

    Mirrors what an SSE/JSON-RPC client gets when it sends `tools/list`.
    The handler reads the bearer from the `request_auth_token`
    ContextVar (set by the HTTP middleware in real traffic; set
    directly here).
    """
    from agent_mcp.app.main_app import mcp_app_instance

    handler = mcp_app_instance.request_handlers[mcp_types.ListToolsRequest]
    req = mcp_types.ListToolsRequest(method="tools/list")
    result = await handler(req)
    inner = result.root if hasattr(result, "root") else result
    return list(getattr(inner, "tools", []) or [])


def _names(tools: list[mcp_types.Tool]) -> set[str]:
    return {t.name for t in tools}


# --- Access classification: the registry must classify every tool ---

def test_every_registered_tool_has_access_classification() -> None:
    """Every name in `tool_schemas` must have an entry in
    `TOOL_ACCESS` (i.e. an explicit admin/any/worker-if-toggled
    decision). New tools without a classification would default to
    "any", which is the most permissive — making the classification
    mandatory catches that omission.
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

ADMIN_ONLY = {
    "create_agent",
    "view_status",
    "terminate_agent",
    "view_audit_log",
    "get_agent_tokens",
    "relaunch_agent",
    "broadcast_admin_message",
    "bulk_task_operations",
    "delete_task",
    "backup_project_context",
}


def test_admin_bearer_sees_every_registered_tool(client) -> None:
    """Admin role → no filter; tools/list returns the full catalogue."""
    from agent_mcp.tools.registry import request_auth_token, tool_schemas

    admin = _admin_token(client)
    token = request_auth_token.set(admin)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    names = _names(tools)
    registered = {e["name"] for e in tool_schemas}
    assert names == registered, (
        f"admin should see every registered tool; missing="
        f"{sorted(registered - names)}, extra={sorted(names - registered)}"
    )


def test_worker_bearer_does_not_see_admin_only_tools(client) -> None:
    """Worker role → all "admin"-classified tools are filtered out."""
    from agent_mcp.tools.registry import request_auth_token

    _admin_token(client)  # trigger lazy init
    worker_tok, _ = _seed_worker("alice-filter")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    names = _names(tools)
    leaked = names & ADMIN_ONLY
    assert not leaked, (
        f"worker tools/list leaked admin-only tools: {sorted(leaked)}"
    )


def test_worker_bearer_sees_any_tools(client) -> None:
    """Worker role → "any"-classified tools remain visible."""
    from agent_mcp.tools.registry import request_auth_token

    _admin_token(client)
    worker_tok, _ = _seed_worker("alice-any")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

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
        f"worker tools/list dropped expected 'any' tools: {sorted(missing)}"
    )


def test_worker_send_agent_message_hidden_by_default(client) -> None:
    """`send_agent_message` is gated on `config_allow_worker_to_worker`
    (default False per PR #16). Worker should NOT see it until the
    toggle is on.
    """
    from agent_mcp.tools.registry import request_auth_token

    _admin_token(client)
    worker_tok, _ = _seed_worker("alice-w2w")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "send_agent_message" not in _names(tools), (
        "worker should not see send_agent_message when "
        "config_allow_worker_to_worker is unset (default False)"
    )


def test_worker_send_agent_message_visible_when_toggle_on(client) -> None:
    """Flip `config_allow_worker_to_worker=true` and the tool appears."""
    from agent_mcp.tools.registry import request_auth_token

    admin = _admin_token(client)
    _set_toggle(client, "config_allow_worker_to_worker", "true", admin)
    worker_tok, _ = _seed_worker("alice-w2w-on")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "send_agent_message" in _names(tools), (
        "worker should see send_agent_message when "
        "config_allow_worker_to_worker=true"
    )


def test_worker_update_task_status_visible_by_default(client) -> None:
    """`update_task_status` is toggle-gated but defaults to True
    (PR #18) — worker should see it without any explicit toggle.
    """
    from agent_mcp.tools.registry import request_auth_token

    _admin_token(client)
    worker_tok, _ = _seed_worker("alice-uts")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "update_task_status" in _names(tools)


def test_worker_update_task_status_hidden_when_toggle_off(client) -> None:
    """Flip `config_allow_worker_update_own_status=false` → hidden."""
    from agent_mcp.tools.registry import request_auth_token

    admin = _admin_token(client)
    _set_toggle(
        client, "config_allow_worker_update_own_status", "false", admin
    )
    worker_tok, _ = _seed_worker("alice-uts-off")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "update_task_status" not in _names(tools)


def test_worker_assign_task_visible_by_default(client) -> None:
    """`assign_task` defaults — both `self_assign` and
    `create_unassigned` default True. With either toggle truthy the
    tool is visible to workers.
    """
    from agent_mcp.tools.registry import request_auth_token

    _admin_token(client)
    worker_tok, _ = _seed_worker("alice-assign")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "assign_task" in _names(tools)


def test_worker_assign_task_hidden_when_both_toggles_off(client) -> None:
    """When BOTH `config_allow_worker_self_assign` and
    `config_allow_worker_create_unassigned` are false, the worker
    can't usefully call `assign_task` — hide it.
    """
    from agent_mcp.tools.registry import request_auth_token

    admin = _admin_token(client)
    _set_toggle(client, "config_allow_worker_self_assign", "false", admin)
    _set_toggle(
        client, "config_allow_worker_create_unassigned", "false", admin
    )
    worker_tok, _ = _seed_worker("alice-assign-off")

    token = request_auth_token.set(worker_tok)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    assert "assign_task" not in _names(tools)


def test_unauthenticated_sees_only_any_tools(client) -> None:
    """No bearer set → conservative: show only "any" tools."""
    from agent_mcp.tools.registry import request_auth_token
    from agent_mcp.tools.access import TOOL_ACCESS

    _admin_token(client)  # trigger lazy init, but don't set ContextVar

    token = request_auth_token.set(None)
    try:
        tools = asyncio.run(_list_tools_via_framework())
    finally:
        request_auth_token.reset(token)

    names = _names(tools)
    expected_any = {n for n, lvl in TOOL_ACCESS.items() if lvl == "any"}
    assert names == expected_any, (
        f"unauthenticated should see exactly 'any' tools; "
        f"missing={sorted(expected_any - names)}, "
        f"extra={sorted(names - expected_any)}"
    )
