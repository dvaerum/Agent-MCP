"""Ambient unread-message nudge for agent callers.

See ``agent_mcp/core/unread_nudge.py``. When an *agent bearer* makes any
MCP tool call and has unread messages, a single advisory line is appended
to that tool's response text so the agent notices + reads its inbox
during normal work — no polling. These tests pin the contract:

* an agent with unread messages sees the nudge on a checkpoint tool call,
* the same agent at zero unread sees nothing,
* a NON-checkpoint tool (read/RAG-style) never carries the nudge,
* an operator caller never sees it (agent-only gate),
* the ``get_agent_messages`` read tool never carries the nudge,
* an unread-lookup failure is swallowed (the nudge never breaks a call).

The first four drive the real MCP framework handler through
``tests/harness.py`` (same path an SSE/JSON-RPC client takes); the gate
and fail-safe cases that are awkward to reach over the wire (operator
principal, injected DB failure) exercise the helper directly.
"""

from __future__ import annotations

import datetime as _dt

import mcp.types as mcp_types
import pytest

from tests.harness import make_principal, mcp_session

_NUDGE_MARK = "unread message(s)"
_READ_TOOL = "get_agent_messages"


def _all_text(blocks) -> str:
    return "\n".join(
        b.text for b in blocks if isinstance(getattr(b, "text", None), str)
    )


def _seed_unread(message_id: str, sender_id: str, recipient_id: str) -> None:
    """Seed one UNREAD agent_messages row (read=0) via raw SQL."""
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO agent_messages "
            "(message_id, sender_id, recipient_id, message_content, "
            " message_type, priority, timestamp, delivered, read) "
            "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 1, 0)",
            (message_id, sender_id, recipient_id, f"hi {recipient_id}", now),
        )
        conn.commit()
    finally:
        conn.close()


# --- Integration: real MCP wire path via the harness ------------------


@pytest.mark.asyncio
async def test_agent_with_unread_gets_nudge(tmp_path) -> None:
    """An agent with unread messages: a checkpoint tool call's response
    text ends with the advisory line naming the count, the senders, and
    the ``get_agent_messages`` read tool.
    """
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        _seed_unread("m1", sender_id="manager", recipient_id="bob")
        _seed_unread("m2", sender_id="ios-app-dev", recipient_id="bob")
        _seed_unread("m3", sender_id="manager", recipient_id="bob")

        blocks = await bob.call("view_tasks", {})
        assert not getattr(bob, "_last_is_error", False), _all_text(blocks)

        # The nudge is the trailing block.
        last = blocks[-1].text
        assert _NUDGE_MARK in last, last
        assert "3" in last, last  # three unread rows
        assert _READ_TOOL in last, last
        # Best-effort senders (two distinct) named in the line.
        assert "manager" in last, last
        assert "ios-app-dev" in last, last


@pytest.mark.asyncio
async def test_agent_with_zero_unread_no_nudge(tmp_path) -> None:
    """The same agent shape with zero unread messages: no nudge line."""
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")

        blocks = await bob.call("view_tasks", {})
        assert not getattr(bob, "_last_is_error", False), _all_text(blocks)
        assert _NUDGE_MARK not in _all_text(blocks), _all_text(blocks)


@pytest.mark.asyncio
async def test_get_agent_messages_call_has_no_nudge(tmp_path) -> None:
    """The read tool itself never carries the nudge (not a checkpoint tool
    — and nudging it would be redundant + racy since reading marks the
    messages read as a side effect).
    """
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        _seed_unread("m1", sender_id="manager", recipient_id="bob")

        blocks = await bob.call(
            _READ_TOOL, {"include_received": True, "mark_as_read": False}
        )
        assert not getattr(bob, "_last_is_error", False), _all_text(blocks)
        # No "You have N unread message(s)" advisory on the read tool.
        assert "You have" not in _all_text(blocks), _all_text(blocks)


# --- Helper-level: checkpoint gate + agent-only gate + fail-safe ------


def _agent_principal(agent_id: str = "bob"):
    return make_principal(
        kind="agent_bearer", agent_id=agent_id, agent_role="worker"
    )


def _operator_principal():
    return make_principal(
        kind="operator_session",
        user_id="alice",
        project_name="proj",
        project_role="operator",
    )


def test_checkpoint_tool_gate(monkeypatch) -> None:
    """Same agent, same unread, two tools: a checkpoint tool
    (``wait_for_events``) nudges; a non-checkpoint tool
    (``ask_project_rag``) does not.
    """
    import agent_mcp.repositories.message_repository as repo

    monkeypatch.setattr(repo, "count_unread_for_recipient", lambda _rid: 3)
    monkeypatch.setattr(
        repo,
        "distinct_unread_senders_for_recipient",
        lambda _rid, limit=3: ["manager"],
    )
    from agent_mcp.core.unread_nudge import maybe_append_unread_nudge

    original = [mcp_types.TextContent(type="text", text="tool output")]

    nudged = maybe_append_unread_nudge(
        original, principal=_agent_principal(), tool_name="wait_for_events"
    )
    assert nudged is not original
    assert _NUDGE_MARK in _all_text(nudged)
    assert "manager" in _all_text(nudged)

    plain = maybe_append_unread_nudge(
        original, principal=_agent_principal(), tool_name="ask_project_rag"
    )
    assert plain is original
    assert _NUDGE_MARK not in _all_text(plain)


def test_operator_bearer_gets_no_nudge(monkeypatch) -> None:
    """An operator (not an agent bearer), even with unread messages in the
    project, never sees the nudge — the agent-only gate short-circuits
    before the count query runs.
    """
    import agent_mcp.repositories.message_repository as repo

    # If the gate leaked, the (monkeypatched) count would produce a nudge.
    monkeypatch.setattr(repo, "count_unread_for_recipient", lambda _rid: 5)
    from agent_mcp.core.unread_nudge import maybe_append_unread_nudge

    original = [mcp_types.TextContent(type="text", text="tool output")]
    out = maybe_append_unread_nudge(
        original, principal=_operator_principal(), tool_name="view_tasks"
    )
    assert out is original
    assert _NUDGE_MARK not in _all_text(out)


def test_unread_lookup_raising_is_swallowed(monkeypatch) -> None:
    """If the unread-count lookup throws, the failure is swallowed and the
    original response is returned unchanged — the nudge never breaks a
    tool call.
    """
    import agent_mcp.repositories.message_repository as repo

    def _boom(_rid):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(repo, "count_unread_for_recipient", _boom)
    from agent_mcp.core.unread_nudge import maybe_append_unread_nudge

    original = [mcp_types.TextContent(type="text", text="tool output")]
    out = maybe_append_unread_nudge(
        original, principal=_agent_principal(), tool_name="view_tasks"
    )
    assert out is original
    assert _NUDGE_MARK not in _all_text(out)
