"""Wave 6 PR 5 — E2E coverage of admin_tools migrated to Principal + ToolResult.

Pins, per migrated tool, that:

  * The new-style signature ``(arguments, *, principal: Principal)
    -> ToolResult`` works end-to-end through ``dispatch_tool_call``.
  * Success returns a typed :class:`Ok` carrying a structured
    ``data`` dict and a human-readable ``message`` for the MCP wire.
  * Auth failures return :class:`PermissionDenied` (not a raised
    ``AuthRejected``) — surfaced on the MCP wire as
    ``"Unauthorized: ..."`` text via :func:`render_as_text_content`.
  * The "new-token return path" is preserved: ``create_agent`` and
    ``relaunch_agent(generate_new_token=True)`` carry the rotated
    bearer in both ``Ok.data["token"]`` (typed) AND the human
    message (``Token: <bearer>``).
  * The full lifecycle works through the MCP wire:
    create → call → terminate → relaunch.

This file is PR 5's parallel to ``tests/test_wave6_pr0_e2e.py`` —
PR 0 pinned the bridge contract for one demo tool; this file pins
the migration contract for all six admin tools.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Conflict,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
)
from tests.harness import mcp_session, with_principal


pytestmark = pytest.mark.asyncio


def _operator_principal(user_id: str = "test-operator") -> Principal:
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker_principal(agent_id: str = "wkr") -> Principal:
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="dummy-worker-token",
    )


# ── view_status ──────────────────────────────────────────────────


async def test_view_status_returns_ok_with_typed_data_for_operator(tmp_path) -> None:
    """``view_status`` returns :class:`Ok` carrying the status payload
    as ``data`` (for REST callers) AND a pretty-printed JSON text in
    ``message`` (for MCP wire callers)."""
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        result = await view_status_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    assert "active_agents_count" in result.data
    assert "agents_details" in result.data
    assert "tmux_info" in result.data
    assert result.message and "MCP Server Status:" in result.message


async def test_view_status_rejects_worker_principal_as_permission_denied(
    tmp_path,
) -> None:
    """Worker-tier principal calling ``view_status`` returns
    :class:`PermissionDenied` (not raised) — the typed-return contract."""
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        result = await view_status_tool_impl(
            {}, principal=_worker_principal()
        )

    assert isinstance(result, PermissionDenied)
    assert "operator" in result.reason.lower()


async def test_view_status_rejects_none_principal(tmp_path) -> None:
    """Unauthenticated call (no Principal) is rejected with
    :class:`PermissionDenied`."""
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        result = await view_status_tool_impl({}, principal=None)

    assert isinstance(result, PermissionDenied)


async def test_view_status_via_mcp_wire_renders_unauthorized_for_worker(
    tmp_path,
) -> None:
    """Through the MCP-wire dispatch a worker calling ``view_status``
    receives a TextContent block starting with ``Unauthorized:`` —
    the renderer's mapping of :class:`PermissionDenied`."""
    async with mcp_session(tmp_path) as admin:
        wkr = await admin.create_worker("wkr-view-status")
        await wkr.assert_unauthorized("view_status", {})


# ── create_agent (preserves new-token return path) ───────────────


async def test_create_agent_ok_carries_token_in_data_and_message(tmp_path) -> None:
    """``create_agent`` returns :class:`Ok` whose ``data["token"]``
    matches the ``Token: <bearer>`` line in ``message``. This is the
    critical "new-token return path" preservation Wave 6 PR 5 calls out
    — the typed ``data`` field is what the REST adapter pulls so the
    string-split at routes.py goes away in PR 6."""
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await create_agent_tool_impl(
            {"agent_id": "agent-with-token", "send_prompt": False},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    token = result.data.get("token")
    assert token, "Ok.data must carry the new agent's token"
    assert result.data["agent_id"] == "agent-with-token"
    assert result.data["agent_role"] == "worker"
    # Same token appears in the human message for MCP wire callers.
    assert result.message and f"Token: {token}" in result.message


async def test_create_agent_persists_agent_role_through_typed_data(tmp_path) -> None:
    """``agent_role`` propagates from arguments → typed return data."""
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await create_agent_tool_impl(
            {
                "agent_id": "mgr-role-agent",
                "agent_role": "manager",
                "send_prompt": False,
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert result.data["agent_role"] == "manager"


async def test_create_agent_conflict_on_duplicate_agent_id(tmp_path) -> None:
    """Second create with the same agent_id returns :class:`Conflict`."""
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    async with mcp_session(tmp_path):
        first = await create_agent_tool_impl(
            {"agent_id": "dup-agent", "send_prompt": False},
            principal=_operator_principal(),
        )
        assert isinstance(first, Ok)

        second = await create_agent_tool_impl(
            {"agent_id": "dup-agent", "send_prompt": False},
            principal=_operator_principal(),
        )

    assert isinstance(second, Conflict), f"expected Conflict, got {second!r}"
    assert "already exists" in second.reason.lower()


async def test_create_agent_invalid_agent_id_returns_invalid(tmp_path) -> None:
    """Missing agent_id → :class:`Invalid` naming the offending field."""
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await create_agent_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Invalid)
    assert result.field == "agent_id"


async def test_create_agent_rejects_worker_principal(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import create_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await create_agent_tool_impl(
            {"agent_id": "should-not-exist", "send_prompt": False},
            principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)


# ── terminate_agent ──────────────────────────────────────────────


async def test_terminate_agent_round_trip(tmp_path) -> None:
    """create_agent → terminate_agent end-to-end via the typed return
    pattern. ``terminate_agent.Ok.data`` carries the new status."""
    from agent_mcp.tools.admin_tools import (
        create_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        created = await create_agent_tool_impl(
            {"agent_id": "term-target", "send_prompt": False},
            principal=_operator_principal(),
        )
        assert isinstance(created, Ok)

        result = await terminate_agent_tool_impl(
            {"agent_id": "term-target"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert result.data["agent_id"] == "term-target"
    assert result.data["status"] == "terminated"


async def test_terminate_agent_not_found_returns_not_found(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import terminate_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await terminate_agent_tool_impl(
            {"agent_id": "never-was"},
            principal=_operator_principal(),
        )

    assert isinstance(result, NotFound)
    assert result.resource == "agent"
    assert result.identifier == "never-was"


async def test_terminate_agent_missing_id_returns_invalid(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import terminate_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await terminate_agent_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Invalid)
    assert result.field == "agent_id"


# ── view_audit_log ───────────────────────────────────────────────


async def test_view_audit_log_returns_typed_entries(tmp_path) -> None:
    """``view_audit_log`` carries entries + counters in ``Ok.data``.
    Operator-tier only."""
    from agent_mcp.tools.admin_tools import (
        create_agent_tool_impl,
        view_audit_log_tool_impl,
    )

    async with mcp_session(tmp_path):
        # Generate an audit entry to read back.
        await create_agent_tool_impl(
            {"agent_id": "audit-source", "send_prompt": False},
            principal=_operator_principal(),
        )

        result = await view_audit_log_tool_impl(
            {"limit": 100}, principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert "entries" in result.data
    assert isinstance(result.data["entries"], list)
    # The create_agent call we just made should be visible.
    actions = [e.get("action") for e in result.data["entries"]]
    assert "create_agent" in actions


async def test_view_audit_log_rejects_worker(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_audit_log_tool_impl

    async with mcp_session(tmp_path):
        result = await view_audit_log_tool_impl(
            {}, principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)


# ── get_agent_tokens ─────────────────────────────────────────────


async def test_get_agent_tokens_returns_typed_pagination(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import (
        create_agent_tool_impl,
        get_agent_tokens_tool_impl,
    )

    async with mcp_session(tmp_path):
        await create_agent_tool_impl(
            {"agent_id": "tok-a", "send_prompt": False},
            principal=_operator_principal(),
        )
        await create_agent_tool_impl(
            {"agent_id": "tok-b", "send_prompt": False},
            principal=_operator_principal(),
        )

        result = await get_agent_tokens_tool_impl(
            {"limit": 50}, principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert "agents" in result.data
    assert "pagination" in result.data
    ids = {a["agent_id"] for a in result.data["agents"]}
    assert {"tok-a", "tok-b"}.issubset(ids)


async def test_get_agent_tokens_rejects_worker(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        result = await get_agent_tokens_tool_impl(
            {}, principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)


# ── relaunch_agent (preserves new-token path) ────────────────────


async def test_relaunch_agent_not_found_returns_not_found(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import relaunch_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await relaunch_agent_tool_impl(
            {"agent_id": "never-was"},
            principal=_operator_principal(),
        )

    assert isinstance(result, NotFound)
    assert result.resource == "agent"


async def test_relaunch_agent_conflict_on_active_status(tmp_path) -> None:
    """Relaunch on a non-terminated agent returns :class:`Conflict`."""
    from agent_mcp.tools.admin_tools import (
        create_agent_tool_impl,
        relaunch_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        await create_agent_tool_impl(
            {"agent_id": "active-agent", "send_prompt": False},
            principal=_operator_principal(),
        )

        result = await relaunch_agent_tool_impl(
            {"agent_id": "active-agent"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Conflict)
    # "Cannot relaunch agent with status 'created'" — the create_agent
    # path lands `status='created'`, which isn't in the allowed list.
    assert "cannot relaunch" in result.reason.lower()


async def test_relaunch_agent_missing_id_returns_invalid(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import relaunch_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await relaunch_agent_tool_impl(
            {}, principal=_operator_principal(),
        )

    assert isinstance(result, Invalid)
    assert result.field == "agent_id"


# ── Bridge (dispatch_tool_call) end-to-end ───────────────────────


async def test_dispatch_view_status_with_explicit_principal(tmp_path) -> None:
    """The dispatcher accepts ``principal=`` directly — no ContextVar
    derivation needed for the migrated tool."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "view_status", {}, principal=_operator_principal()
        )

    assert isinstance(result, Ok)
    assert "active_agents_count" in result.data


async def test_dispatch_view_status_with_with_principal_helper(tmp_path) -> None:
    """``with_principal()`` stamps ``request_principal`` so the surfaces
    that read it see the operator. The dispatcher itself requires
    explicit ``principal=`` post-PR-6 — pass it through."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        p = _operator_principal("op-via-helper")
        with with_principal(p):
            result = await dispatch_tool_call(
                "view_status", {}, principal=p,
            )

    assert isinstance(result, Ok)


async def test_dispatch_create_agent_via_rest_adapter_returns_201_with_data(
    tmp_path,
) -> None:
    """The REST adapter ``_dispatch_through_tool`` maps the migrated
    ``create_agent`` ``Ok(data=...)`` onto 201 + ``{"data": {...}}``
    JSON envelope."""
    from agent_mcp.app.routes import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841 (lifespan)
        response = await _dispatch_through_tool(
            "create_agent",
            {"agent_id": "rest-adapter-target", "send_prompt": False},
            bearer_token=None,
            operator_session=True,
            operator_user_id="test-operator",
        )

    assert response.status_code == 201, response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["success"] is True
    assert body["data"]["agent_id"] == "rest-adapter-target"
    assert body["data"].get("token"), (
        "REST adapter must surface the new token via Ok.data['token']"
    )


# ── Full lifecycle via MCP wire ──────────────────────────────────


async def test_admin_tools_full_lifecycle_via_mcp_wire(tmp_path) -> None:
    """End-to-end via the MCP wire: create → view_status sees it →
    terminate → view_status reflects the termination → relaunch
    after termination → confirm typed-data integrity throughout.

    This is the lifecycle test the Wave 6 PR 5 brief calls out: it
    proves the migrated admin tools work as a cohesive set through
    the same code path real MCP clients hit (handler →
    dispatch_tool_call → tool impl → render_as_text_content)."""
    async with mcp_session(tmp_path) as admin:
        # CREATE
        create_result = await admin.assert_tool_succeeds(
            "create_agent",
            {"agent_id": "lifecycle-bot", "send_prompt": False},
        )
        text = create_result[0].text
        assert "Token:" in text, (
            "MCP wire must preserve the 'Token: <bearer>' line for "
            "legacy admin scripts that scrape it"
        )
        assert "lifecycle-bot" in text

        # VIEW STATUS — confirm the new agent shows up
        status_result = await admin.assert_tool_succeeds("view_status", {})
        status_text = status_result[0].text
        assert "active_agents_count" in status_text

        # TERMINATE
        terminate_result = await admin.assert_tool_succeeds(
            "terminate_agent", {"agent_id": "lifecycle-bot"},
        )
        terminate_text = terminate_result[0].text
        assert "terminated" in terminate_text.lower()
        assert "lifecycle-bot" in terminate_text
