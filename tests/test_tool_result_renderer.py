"""Unit coverage of :func:`agent_mcp.core.tool_result.render_as_text_content`.

The MCP wire renderer is the single conversion surface from the
typed ``ToolResult`` sum type to the legacy ``list[TextContent]``
shape MCP clients consume (Wave 6 PR 6 deleted the bridge that
used to live next to ``dispatch_tool_call``; the renderer is
authoritative).

These tests pin the "Ok with both message and data" case — the
F015 regression that silently dropped ``data`` whenever ``message``
was set. ``register_agent`` returns
``Ok(data={"agent_id":..., "token":..., "mcp_snippet":...},
message="Agent ... registered. Paste the snippet ...")`` and
MCP-wire callers got only the prose; the actionable token + snippet
were dropped on the floor.

MCP spec rev 2025-03-26 allows a ``tools/call`` response to carry
multiple content blocks (``content: [TextContent, ...]``); we use
that to ship both the prose summary AND the JSON-serialised payload.
"""

from __future__ import annotations

import json

import mcp.types as mcp_types

from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    render_as_text_content,
)

# ── Ok variant — the F015 regression surface ─────────────────────


def test_ok_with_message_and_data_emits_two_blocks() -> None:
    """The F015 fix: when an ``Ok`` carries BOTH ``message`` and
    ``data``, the renderer emits TWO ``TextContent`` blocks — message
    first as the prose summary, JSON-serialised data second as the
    actionable payload. Pre-fix the renderer treated ``message`` as
    winner-takes-all and silently dropped ``data``, so
    ``register_agent``'s token + snippet never reached the MCP
    client.
    """
    blocks = render_as_text_content(
        Ok(
            data={"token": "abc-123", "agent_id": "worker-1"},
            message="Agent worker-1 registered.",
        )
    )

    assert len(blocks) == 2, (
        f"expected 2 TextContent blocks (message + data); got "
        f"{len(blocks)}: {[b.text for b in blocks]!r}"
    )
    assert isinstance(blocks[0], mcp_types.TextContent)
    assert isinstance(blocks[1], mcp_types.TextContent)
    assert blocks[0].text == "Agent worker-1 registered."
    # Second block JSON-decodes to the original data dict — the
    # contract MCP clients can rely on.
    assert json.loads(blocks[1].text) == {
        "token": "abc-123",
        "agent_id": "worker-1",
    }


def test_ok_with_message_and_string_data_does_not_double_encode() -> None:
    """When ``data`` is itself a string the second block carries it
    verbatim, not double-JSON-encoded. A double-encode would wrap
    the string in extra quotes and force every client to ``json.loads``
    twice — the contract is "raw string passes through".
    """
    blocks = render_as_text_content(
        Ok(data="raw-payload", message="ok")
    )

    assert len(blocks) == 2
    assert blocks[0].text == "ok"
    assert blocks[1].text == "raw-payload"


def test_ok_with_message_only_emits_one_block() -> None:
    """Message-only ``Ok`` (the typical "tool succeeded; here's the
    prose" return) still emits exactly one block."""
    blocks = render_as_text_content(Ok(message="done"))
    assert len(blocks) == 1
    assert blocks[0].text == "done"


def test_ok_with_data_only_emits_one_block_json_serialised() -> None:
    """Data-only ``Ok`` emits one block with the JSON serialisation —
    the legacy ``Ok(data=...)`` path the REST adapter also exercises.
    """
    blocks = render_as_text_content(Ok(data={"k": "v"}))
    assert len(blocks) == 1
    assert json.loads(blocks[0].text) == {"k": "v"}


def test_ok_with_string_data_only_emits_one_block_verbatim() -> None:
    """String ``data`` (no message) passes through without JSON
    encoding — mirrors the legacy pre-F015 behaviour for the
    data-only path so tools that returned ``Ok(data="...")`` are
    bit-identical on the wire."""
    blocks = render_as_text_content(Ok(data="plain text"))
    assert len(blocks) == 1
    assert blocks[0].text == "plain text"


def test_ok_empty_emits_one_empty_block() -> None:
    """``Ok()`` with neither field set keeps the legacy "empty status"
    shape so tools that returned bare ``Ok()`` don't suddenly emit a
    different number of content blocks."""
    blocks = render_as_text_content(Ok())
    assert len(blocks) == 1
    assert blocks[0].text == ""


def test_ok_with_non_json_serialisable_data_falls_back_to_str() -> None:
    """Defensive: a tool that returns ``data`` containing a type
    ``json.dumps`` can't handle still produces a TextContent block
    rather than crashing the renderer. ``default=str`` handles most
    exotic types; the bare-``str()`` fallback covers the rest.
    """

    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"

    blocks = render_as_text_content(Ok(data=_Weird(), message="x"))
    # Renders without raising — that's the contract; the exact text
    # of the second block depends on json.dumps's default-str path.
    assert len(blocks) == 2
    assert blocks[0].text == "x"
    assert "<weird>" in blocks[1].text


# ── Register-agent shape regression ──────────────────────────────


def test_register_agent_shape_round_trip() -> None:
    """End-to-end pin for the exact shape ``register_agent_tool_impl``
    returns: ``Ok(data={...token, mcp_snippet...}, message="...paste
    the snippet...")``. The renderer MUST surface both halves; this is
    the contract F015 broke and the F015 fix restores.
    """
    snippet = (
        '{"mcpServers": {"agent-mcp-demo": {"type": "http", '
        '"url": "https://h.x/agent-mcp/mcp/demo", "headers": '
        '{"Authorization": "Bearer t-xyz"}}}}'
    )
    blocks = render_as_text_content(
        Ok(
            data={
                "agent_id": "worker-1",
                "token": "t-xyz",
                "agent_role": "worker",
                "mcp_snippet": snippet,
                "project_name": "demo",
            },
            message=(
                "Agent 'worker-1' registered. Paste the snippet into "
                "the user's claude .mcp.json — agent-mcp no longer "
                "spawns the claude session itself."
            ),
        )
    )

    assert len(blocks) == 2
    # The MCP client sees the prose first.
    assert "registered" in blocks[0].text.lower()
    # And the actionable payload second.
    payload = json.loads(blocks[1].text)
    assert payload["token"] == "t-xyz"
    assert payload["agent_id"] == "worker-1"
    # mcp_snippet survives intact (the operator pastes it into the
    # user's claude .mcp.json).
    assert payload["mcp_snippet"] == snippet


# ── Error variants — pinned for completeness ─────────────────────


def test_not_found_emits_error_prefixed_block() -> None:
    blocks = render_as_text_content(NotFound(resource="task", identifier="42"))
    assert len(blocks) == 1
    assert blocks[0].text.startswith("Error:")
    assert "'42'" in blocks[0].text


def test_permission_denied_emits_unauthorized_block() -> None:
    blocks = render_as_text_content(PermissionDenied(reason="not the author"))
    assert len(blocks) == 1
    assert blocks[0].text.startswith("Unauthorized:")


def test_invalid_with_field_emits_field_specific_block() -> None:
    blocks = render_as_text_content(Invalid(field="text", message="empty"))
    assert len(blocks) == 1
    assert "invalid text" in blocks[0].text


def test_invalid_without_field_emits_generic_block() -> None:
    blocks = render_as_text_content(Invalid(message="bad shape"))
    assert len(blocks) == 1
    assert "invalid input" in blocks[0].text


def test_conflict_and_failed_emit_error_blocks() -> None:
    assert render_as_text_content(Conflict(reason="dup"))[0].text.startswith(
        "Error: conflict:"
    )
    assert render_as_text_content(Failed(message="boom"))[0].text.startswith(
        "Error:"
    )
