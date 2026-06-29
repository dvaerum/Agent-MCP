"""Wave 6 PR 2 — E2E coverage of the migrated agent-communication tools.

Mirrors the structure of :mod:`tests.test_wave6_pr0_e2e` but covers
the five tools migrated in PR 2:

* ``send_agent_message``
* ``get_agent_messages``
* ``broadcast_admin_message``
* ``wait_for_events``
* ``fetch_events_since``

Each test drives the full seam (HTTP-equivalent dispatch →
:class:`~agent_mcp.core.principal.Principal` → tool → typed
:class:`~agent_mcp.core.tool_result.ToolResult` → renderer / REST
adapter). The PR-0 pattern is reused: tools called via
:func:`agent_mcp.tools.registry.dispatch_tool_call` with an explicit
``principal=`` kwarg (post-migration contract), and via the MCP
wire path ``admin.call(...)`` which crosses the bridge.

The sender-attribution preservation called out in the PR brief is
pinned by the broadcast and send tests: when the principal carries
an ``agent_id`` it shows up verbatim on the persisted message row.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Invalid,
    Ok,
    PermissionDenied,
)
from tests.harness import mcp_session, with_principal

pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────


def _operator_principal(user_id: str = "alice") -> Principal:
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name="demo",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker_principal(agent_id: str, token: str) -> Principal:
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token=token,
    )


# ── send_agent_message ──────────────────────────────────────────


async def test_send_agent_message_via_dispatch_returns_ok_with_data(
    tmp_path,
) -> None:
    """Dispatcher → ``send_agent_message`` with an operator principal
    returns ``Ok(data={...}, message=...)`` with the message_id,
    sender, recipient, and delivery_status on the data field.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        result = await dispatch_tool_call(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hello via dispatch",
                "deliver_method": "store",
            },
            principal=_operator_principal("op-alice"),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    assert result.data["recipient_id"] == "alice"
    assert result.data["delivery_status"] == "stored"
    # Sender attribution: operator principal has no agent_id; user_id
    # is the headline label. The PR brief locks this in as the new
    # contract ("principal.agent_id or 'operator'", with user_id as a
    # specificity fallback).
    assert result.data["sender"] == "op-alice"
    assert result.data["message_id"].startswith("msg_")
    assert "alice" in (result.message or "")


async def test_send_agent_message_invalid_args_returns_invalid(tmp_path) -> None:
    """Missing message → ``Invalid(field='message')`` (NOT a textual
    error string). Pins the typed-error contract per the migration
    spec — the dashboard/REST adapter relies on the field name.

    Bypasses jsonschema (``additionalProperties: False`` + required
    list) by calling the impl directly with a Principal — the spec
    is "tool's own validation"; jsonschema would otherwise reject
    the missing required field upstream of the tool.
    """
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        result = await send_agent_message_tool_impl(
            {"recipient_id": "alice"},
            principal=_operator_principal(),
        )

    assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"
    assert result.field == "message"


async def test_send_agent_message_worker_denied_without_toggle(
    tmp_path,
) -> None:
    """A worker bearer with the ``config_allow_worker_to_worker``
    toggle off receives ``PermissionDenied`` from the inline gate
    (formerly the ``@requires_policy`` decorator).
    """
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        result = await send_agent_message_tool_impl(
            {
                "recipient_id": "alice",
                "message": "hi alice",
                "deliver_method": "store",
            },
            principal=_worker_principal("bob", bob.token),
        )

    assert isinstance(result, PermissionDenied), (
        f"expected PermissionDenied, got {result!r}"
    )
    assert "config_allow_worker_to_worker" in result.reason


async def test_send_agent_message_via_mcp_wire_renders_text(tmp_path) -> None:
    """An MCP-wire call (admin bearer through the registered handler)
    receives the legacy ``[TextContent]`` rendering of
    ``Ok(message=...)`` via :func:`render_as_text_content`. Pins
    that MCP clients see no behavioural change post-migration.
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        result = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "hello mcp",
                "deliver_method": "store",
            },
        )
        text = result[0].text
        assert "Message sent to alice" in text
        assert "Message stored" in text


# ── get_agent_messages ──────────────────────────────────────────


async def test_get_agent_messages_returns_ok_with_messages_list(
    tmp_path,
) -> None:
    """``get_agent_messages`` returns
    ``Ok(data={"messages": [...], "count": N, "agent_id": ...})`` —
    a typed payload the REST adapter can serve verbatim.
    """
    from agent_mcp.tools.agent_communication_tools import (
        get_agent_messages_tool_impl,
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await send_agent_message_tool_impl(
            {
                "recipient_id": "alice",
                "message": "for alice",
                "deliver_method": "store",
            },
            principal=_operator_principal(),
        )

        result = await get_agent_messages_tool_impl(
            {},
            principal=_worker_principal("alice", alice.token),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data["agent_id"] == "alice"
    assert result.data["count"] == 1
    assert result.data["messages"][0]["message_content"] == "for alice"


async def test_get_agent_messages_no_principal_denies(tmp_path) -> None:
    """Without an identifying Principal (and no token fallback),
    the tool returns ``PermissionDenied`` — the gate that replaced
    ``@requires("any")`` is principal-driven.
    """
    from agent_mcp.tools.agent_communication_tools import (
        get_agent_messages_tool_impl,
    )

    async with mcp_session(tmp_path):
        # No principal, no token in args, but mcp_session has stamped
        # operator_session_active=True — so the contextvar bridge
        # returns an operator_session Principal with agent_id=None.
        # Without an agent_id we cannot scope messages → denied.
        result = await get_agent_messages_tool_impl({}, principal=None)

    assert isinstance(result, PermissionDenied), (
        f"expected PermissionDenied, got {result!r}"
    )


# ── broadcast_admin_message ─────────────────────────────────────


async def test_broadcast_admin_message_fans_out_with_operator_principal(
    tmp_path,
) -> None:
    """``broadcast_admin_message`` returns ``Ok`` with sent_count and
    the recipients list. The per-recipient fan-out preserves sender
    attribution from the broadcast caller's Principal (verified via
    the message row each worker receives).
    """
    from agent_mcp.tools.agent_communication_tools import (
        broadcast_admin_message_tool_impl,
        get_agent_messages_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        result = await broadcast_admin_message_tool_impl(
            {"message": "ops broadcast"},
            principal=_operator_principal("op-alice"),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data["sent_count"] == 2, result.data
        assert set(result.data["recipients"]) == {"alice", "bob"}

        # Sender attribution preserved through the fan-out: each
        # worker's inbox carries the broadcast with the operator's
        # user_id as the sender (no agent_id on the operator
        # principal).
        for worker, agent_id in ((alice, "alice"), (bob, "bob")):
            msgs = await get_agent_messages_tool_impl(
                {"mark_as_read": False},
                principal=_worker_principal(agent_id, worker.token),
            )
            assert isinstance(msgs, Ok)
            sender_ids = {m["sender_id"] for m in msgs.data["messages"]}
            assert "op-alice" in sender_ids, sender_ids


async def test_broadcast_worker_denied(tmp_path) -> None:
    """A plain worker bearer cannot broadcast — the new gate refuses
    any principal that's not operator-tier (or the legacy ``admin``
    label).
    """
    from agent_mcp.tools.agent_communication_tools import (
        broadcast_admin_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        result = await broadcast_admin_message_tool_impl(
            {"message": "i should not be able to"},
            principal=_worker_principal("bob", bob.token),
        )

    assert isinstance(result, PermissionDenied)


# ── wait_for_events ─────────────────────────────────────────────


async def test_wait_for_events_returns_ok_envelope(tmp_path) -> None:
    """``wait_for_events`` returns
    ``Ok(data={"events": [...], "next_cursor": "..."})`` for an
    agent principal with a pending message. The ``message`` field
    carries the JSON-encoded envelope (preserves the MCP wire
    shape).
    """
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
        wait_for_events_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await send_agent_message_tool_impl(
            {
                "recipient_id": "alice",
                "message": "pending",
                "deliver_method": "store",
            },
            principal=_operator_principal(),
        )

        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=1)
        ).isoformat()
        result = await wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 2},
            principal=_worker_principal("alice", alice.token),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert "events" in result.data
    assert "next_cursor" in result.data
    assert len(result.data["events"]) == 1
    assert result.data["events"][0]["data"]["message_content"] == "pending"

    # Wire shape: the message field is the JSON-encoded envelope so
    # render_as_text_content produces the same bytes pre-Wave-6 callers
    # already parse.
    decoded = json.loads(result.message)
    assert decoded == result.data


async def test_wait_for_events_invalid_since_returns_invalid(tmp_path) -> None:
    """Non-string ``since`` → ``Invalid(field='since')``."""
    from agent_mcp.tools.agent_communication_tools import (
        wait_for_events_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        result = await wait_for_events_tool_impl(
            {"since": 12345, "timeout_seconds": 1},
            principal=_worker_principal("alice", alice.token),
        )

    assert isinstance(result, Invalid)
    assert result.field == "since"


# ── fetch_events_since ──────────────────────────────────────────


async def test_fetch_events_since_returns_ok_envelope(tmp_path) -> None:
    """``fetch_events_since`` returns ``Ok(data={"events", "cursor"})``
    — the catch-up envelope. Pure-DB path (no blocking).
    """
    from agent_mcp.tools.agent_communication_tools import (
        fetch_events_since_tool_impl,
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await send_agent_message_tool_impl(
            {
                "recipient_id": "alice",
                "message": "missed while offline",
                "deliver_method": "store",
            },
            principal=_operator_principal(),
        )

        result = await fetch_events_since_tool_impl(
            {},
            principal=_worker_principal("alice", alice.token),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data["events"]
    assert result.data["cursor"]
    # Returned cursor field name is "cursor" (NOT "next_cursor" — the
    # fetch_events_since spec differs from wait_for_events here).
    assert "cursor" in result.data and "next_cursor" not in result.data


# ── with_principal harness helper ───────────────────────────────


async def test_with_principal_helper_works_for_send_message(
    tmp_path,
) -> None:
    """Wave 6 PR 6: :func:`with_principal` stamps
    :data:`request_principal` for surfaces that consult it; the
    dispatcher itself takes ``principal=`` explicitly. Pass it
    through and confirm the tool sees the operator identity.
    """
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        p = _operator_principal("op-via-with-principal")
        with with_principal(p):
            result = await dispatch_tool_call(
                "send_agent_message",
                {
                    "recipient_id": "alice",
                    "message": "via with_principal",
                    "deliver_method": "store",
                },
                principal=p,
            )

    assert isinstance(result, Ok)
    assert result.data["sender"] == "op-via-with-principal"


# ── Bridge: token fallback for legacy direct-impl callers ───────


async def test_legacy_direct_call_with_token_resolves_to_agent(
    tmp_path,
) -> None:
    """Pre-Wave-6 tests called impls directly with ``token`` in the
    args dict and no Principal in hand. The :func:`_resolve_principal`
    fallback in this module derives an ``agent_bearer`` Principal
    from the token so those callers keep working without a sweep.
    PR 6 deletes this fallback once every caller passes Principal
    explicitly.
    """
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # No principal kwarg, but token in args → bridge fallback
        # resolves it to the admin's manager-tier bearer.
        result = await send_agent_message_tool_impl(
            {
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "via legacy path",
                "deliver_method": "store",
            },
        )

    assert isinstance(result, Ok)
    # The harness seeds the admin token's agent_id as "admin" — the
    # token fallback in _resolve_principal returns an agent_bearer
    # Principal with that agent_id, so sender attribution becomes
    # "admin" (matching the pre-Wave-6 behaviour).
    assert result.data["sender"] == "admin"
