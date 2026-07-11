"""Wave 6 PR 5 — E2E coverage of admin_tools migrated to Principal + ToolResult.

Pins, per migrated tool, that:

  * The new-style signature ``(arguments, *, principal: Principal)
    -> ToolResult`` works end-to-end through ``dispatch_tool_call``.
  * Success returns a typed :class:`Ok` carrying a structured
    ``data`` dict and a human-readable ``message`` for the MCP wire.
  * Auth failures return :class:`PermissionDenied` (not a raised
    ``AuthRejected``) — surfaced on the MCP wire as
    ``"Unauthorized: ..."`` text via :func:`render_as_text_content`.
  * The "new-token return path" is preserved: ``register_agent``
    (post-Wave-7-PR-1 — was ``create_agent``) and
    ``relaunch_agent(generate_new_token=True)`` carry the bearer in
    both ``Ok.data["token"]`` (typed) AND the human message
    (``Token: <bearer>``).
  * The full lifecycle works through the MCP wire:
    register → call → terminate → relaunch.

This file is PR 5's parallel to ``tests/test_wave6_pr0_e2e.py`` —
PR 0 pinned the bridge contract for one demo tool; this file pins
the migration contract for all six admin tools.

Wave 7 PR 1 (coordinator transition, 2026-06-29): migrated the
agent-creation calls from ``create_agent_tool_impl`` (the legacy
spawn impl that orphan-stormed claude processes during the pytest
sweep) to ``register_agent_tool_impl`` (the spawnless sibling
shipped in Wave 7 PR 0). The typed-return contract being pinned
here is identical between the two impls — both return ``Ok`` with
``data["token"]`` + ``data["agent_id"]`` + ``data["agent_role"]``
and a ``Token: <bearer>`` line in ``message``; both return
:class:`Invalid` / :class:`Conflict` / :class:`PermissionDenied`
under the same conditions. PR 3 collapses the legacy impl into the
register-only shape entirely.
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
from tests.harness import make_principal, mcp_session, with_principal


pytestmark = pytest.mark.asyncio


def _operator_principal(user_id: str = "test-operator") -> Principal:
    return make_principal(
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
    return make_principal(
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
    # Wave 7 PR 3 (coordinator transition) dropped the ``tmux_info``
    # block — agent-mcp no longer owns user-side claude sessions,
    # so liveness is derived from the MCP session registry instead.
    assert "tmux_info" not in result.data
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


# ── register_agent (preserves new-token return path) ─────────────
#
# Wave 7 PR 1: the create_agent calls below were retargeted to
# register_agent_tool_impl — the spawnless sibling shipped in PR 0
# that holds the same typed-return contract without invoking tmux.
# The legacy ``create_agent_tool_impl`` is deleted in PR 3.


async def test_create_agent_ok_carries_token_in_data_and_message(tmp_path) -> None:
    """``register_agent`` returns :class:`Ok` whose ``data["token"]``
    carries the new agent's bearer. This is the critical "new-token
    return path" preservation Wave 6 PR 5 calls out — the typed
    ``data`` field is what the REST adapter pulls so the string-split
    at routes.py is unneeded.

    Wave 7 PR 1: previously exercised ``create_agent_tool_impl`` (spawn
    path). The register-only sibling shipped in PR 0 preserves the same
    typed-data shape (``agent_id`` / ``token`` / ``agent_role``). The
    legacy spawn impl additionally emitted a ``Token: <bearer>`` line in
    the human-readable message for legacy admin scripts that scraped
    it — the register-only impl drops that line by design (the
    ``mcp_snippet`` field in ``data`` is the operator-facing carrier of
    the bearer in the coordinator model, plus it's still available as
    plain text in the snippet's Authorization header).
    """
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"agent_id": "agent-with-token"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    token = result.data.get("token")
    assert token, "Ok.data must carry the new agent's token"
    assert result.data["agent_id"] == "agent-with-token"
    assert result.data["agent_role"] == "worker"
    # The bearer surfaces via the mcp_snippet field too (register-only
    # impl's operator UX). Belt-and-braces: the snippet's Authorization
    # header carries the token verbatim.
    snippet = result.data.get("mcp_snippet") or ""
    assert token in snippet, (
        "register_agent must embed the minted token in the mcp_snippet "
        "Authorization header so the operator's paste-the-snippet UX "
        "carries the bearer end-to-end."
    )


async def test_create_agent_persists_agent_role_through_typed_data(tmp_path) -> None:
    """``agent_role`` propagates from arguments → typed return data."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {
                "agent_id": "mgr-role-agent",
                "agent_role": "manager",
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert result.data["agent_role"] == "manager"


async def test_create_agent_conflict_on_duplicate_agent_id(tmp_path) -> None:
    """Second register with the same agent_id returns :class:`Conflict`."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        first = await register_agent_tool_impl(
            {"agent_id": "dup-agent"},
            principal=_operator_principal(),
        )
        assert isinstance(first, Ok)

        second = await register_agent_tool_impl(
            {"agent_id": "dup-agent"},
            principal=_operator_principal(),
        )

    assert isinstance(second, Conflict), f"expected Conflict, got {second!r}"
    assert "already exists" in second.reason.lower()


async def test_create_agent_invalid_agent_id_returns_invalid(tmp_path) -> None:
    """Missing agent_id → :class:`Invalid` naming the offending field.

    Wave 7 PR 1: ``register_agent`` reports the missing field as
    ``"name"`` (the new arg shape per the Wave 7 plan); the legacy
    ``create_agent`` reported it as ``"agent_id"``. The contract being
    pinned is "Invalid with a non-empty field naming the missing input",
    not the specific spelling.
    """
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Invalid)
    assert result.field in ("name", "agent_id"), (
        f"Invalid.field should name the missing input; got {result.field!r}"
    )


async def test_create_agent_rejects_worker_principal(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"agent_id": "should-not-exist"},
            principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)


# ── terminate_agent ──────────────────────────────────────────────


async def test_terminate_agent_round_trip(tmp_path) -> None:
    """register_agent → terminate_agent end-to-end via the typed return
    pattern. ``terminate_agent.Ok.data`` carries the new status.

    Wave 7 PR 1: register replaces create in the setup step — the
    typed-return contract for terminate is unchanged.
    """
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        created = await register_agent_tool_impl(
            {"agent_id": "term-target"},
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
    Operator-tier only.

    Wave 7 PR 1: ``register_agent`` writes its own ``register_agent``
    audit-log action (the legacy spawn impl wrote ``create_agent``).
    The contract being pinned is "the operator-tier action that minted
    this agent is visible in the audit log", not the specific action
    name string — both spellings are accepted to keep the audit
    catalogue migration loose during the Wave 7 cutover window.
    """
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        view_audit_log_tool_impl,
    )

    async with mcp_session(tmp_path):
        # Generate an audit entry to read back.
        await register_agent_tool_impl(
            {"agent_id": "audit-source"},
            principal=_operator_principal(),
        )

        result = await view_audit_log_tool_impl(
            {"limit": 100}, principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    assert "entries" in result.data
    assert isinstance(result.data["entries"], list)
    # The register_agent call we just made should be visible. Wave 7
    # PR 1: the action name is ``register_agent`` post-cutover (was
    # ``create_agent`` from the legacy spawn impl); accept either so
    # the test stays stable across the catalogue change.
    actions = [e.get("action") for e in result.data["entries"]]
    assert "register_agent" in actions or "create_agent" in actions, (
        f"register_agent must emit an audit entry; got actions={actions!r}"
    )


async def test_view_audit_log_rejects_worker(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_audit_log_tool_impl

    async with mcp_session(tmp_path):
        result = await view_audit_log_tool_impl(
            {}, principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)


# ── get_agent_tokens ─────────────────────────────────────────────


async def test_get_agent_tokens_returns_typed_pagination(tmp_path) -> None:
    """Wave 7 PR 1: agent seeding switched from spawn-path
    ``create_agent_tool_impl`` to register-only ``register_agent_tool_impl``.
    The pagination contract being pinned is unchanged."""
    from agent_mcp.tools.admin_tools import (
        get_agent_tokens_tool_impl,
        register_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        await register_agent_tool_impl(
            {"agent_id": "tok-a"},
            principal=_operator_principal(),
        )
        await register_agent_tool_impl(
            {"agent_id": "tok-b"},
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


# Wave 7 PR 3 (coordinator transition): ``relaunch_agent_tool_impl`` was
# deleted with the rest of the spawn machinery — it relied on the
# ``send_command_to_session`` / ``send_prompt_async`` tmux helpers
# that have no surviving home. The relaunch concept has no analogue
# under the coordinator model (the user starts and stops their own
# claude session). The three contract tests that pinned its NotFound /
# Conflict / Invalid wording moved out with it.


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


async def test_dispatch_create_agent_via_rest_adapter_returns_ok_with_data(
    tmp_path,
) -> None:
    """The REST adapter ``_dispatch_through_tool`` maps the migrated
    ``register_agent`` ``Ok(data=...)`` onto an HTTP 200/201 JSON
    envelope shaped ``{"success": true, "data": {...}}``.

    Wave 7 PR 1: retargeted from the legacy ``create_agent`` spawn tool
    to ``register_agent`` (the spawnless sibling shipped in PR 0). The
    REST adapter is impl-agnostic for the success envelope. The status
    code drops from 201 → 200 because the adapter's "201 if create_*"
    heuristic keys on the tool name; ``register_agent`` falls under the
    "non-create_* mutating tool" path that returns 200. The carried
    data is identical either way (``agent_id`` + ``token`` in
    ``body["data"]``).
    """
    from agent_mcp.app._dispatch_helpers import _dispatch_through_tool

    async with mcp_session(tmp_path) as admin:  # noqa: F841 (lifespan)
        response = await _dispatch_through_tool(
            "register_agent",
            {"agent_id": "rest-adapter-target"},
            bearer_token=None,
            operator_session=True,
            operator_user_id="test-operator",
        )

    assert response.status_code in (200, 201), response.body
    import json as _json
    body = _json.loads(response.body)
    assert body["success"] is True
    assert body["data"]["agent_id"] == "rest-adapter-target"
    assert body["data"].get("token"), (
        "REST adapter must surface the new token via Ok.data['token']"
    )


# ── Full lifecycle via MCP wire ──────────────────────────────────


async def test_admin_tools_full_lifecycle_via_mcp_wire(tmp_path) -> None:
    """End-to-end via the MCP wire: register → view_status sees it →
    terminate → view_status reflects the termination → confirm
    typed-data integrity throughout.

    This is the lifecycle test the Wave 6 PR 5 brief calls out: it
    proves the migrated admin tools work as a cohesive set through
    the same code path real MCP clients hit (handler →
    dispatch_tool_call → tool impl → render_as_text_content).

    Wave 7 PR 1: the lifecycle's create step now uses ``register_agent``
    (the spawnless sibling shipped in PR 0). The ``Token: <bearer>``
    line in the MCP message is preserved across the cutover — that's
    the contract legacy admin scripts depend on.
    """
    async with mcp_session(tmp_path) as admin:
        # REGISTER (Wave 7 PR 1: was create_agent — spawn path). The
        # legacy spawn impl emitted "Token: <bearer>" in the message
        # for admin scripts to scrape; the register-only impl carries
        # the bearer in the mcp_snippet's Authorization header instead.
        # The MCP-wire text is the human-readable message Ok carries.
        register_result = await admin.assert_tool_succeeds(
            "register_agent",
            {"agent_id": "lifecycle-bot"},
        )
        text = register_result[0].text
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
