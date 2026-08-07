"""Tools block: `send_agent_message` accepts subject + parent.

The MCP tool layer handles the two agent_messages threading columns.
Specifically:

* `send_agent_message` gains optional `subject` and
  `parent_message_id` arguments.
* When the caller passes `subject`, it is persisted verbatim
  (no LLM call).
* When the caller omits `subject` for a root message
  (`parent_message_id` absent / None), the implementation stores
  `subject = NULL` — Phase 1 (null-subject placeholder). It does NOT
  call `suggest_subject` on the send path (that synchronous model call
  was a latency/RAM problem) and does NOT persist a truncated body; the
  read paths compute a 50-char preview on demand. NULL is the marker.
* When the caller is sending a reply
  (`parent_message_id` is set), `subject` stays NULL regardless of
  config — replies don't carry subjects.

The `suggest_subject` helper is mocked to RAISE where relevant, proving
the send path never invokes it.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _fetch_message_row(db_path: str, message_id: str) -> tuple | None:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT subject, parent_message_id, message_content "
            "FROM agent_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()


def _latest_message_id_for(db_path: str, sender: str, recipient: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT message_id FROM agent_messages "
            "WHERE sender_id = ? AND recipient_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (sender, recipient),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        f"no message rows for sender={sender} recipient={recipient}"
    )
    return row[0]


async def test_explicit_subject_stored_verbatim(tmp_path, monkeypatch) -> None:
    """Explicit subject = verbatim INSERT, no LLM call ever."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    # Trip-wire — if the impl calls the helper, the test fails loudly.
    def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject should not be called when an explicit "
            f"subject is provided; content={content!r}"
        )

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "body here",
                "subject": "Custom Topic",
                "deliver_method": "store",
            },
        )
        msg_id = _latest_message_id_for(str(get_db_path()), "admin", "alice")
        row = _fetch_message_row(str(get_db_path()), msg_id)
        assert row is not None
        subject, parent_id, content = row
        assert subject == "Custom Topic", subject
        assert parent_id is None
        assert content == "body here"


async def test_root_without_subject_stores_null_even_with_model(
    tmp_path, monkeypatch
) -> None:
    """Root + no subject → NULL stored; suggest_subject NEVER called even
    when AGENT_MCP_SUBJECT_MODEL is configured (Phase 1)."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject must NOT be called on the send path; "
            f"content={content!r}"
        )

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "please help with the build",
                "deliver_method": "store",
            },
        )
        msg_id = _latest_message_id_for(str(get_db_path()), "admin", "alice")
        row = _fetch_message_row(str(get_db_path()), msg_id)
        assert row is not None
        subject, parent_id, _content = row
        assert subject is None, f"expected NULL subject, got {subject!r}"
        assert parent_id is None


async def test_root_without_subject_no_model_stores_null(
    tmp_path, monkeypatch
) -> None:
    """Root + no subject + AGENT_MCP_SUBJECT_MODEL unset → NULL stored
    (NOT a truncated body). The preview is computed at read time only."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    monkeypatch.delenv("AGENT_MCP_SUBJECT_MODEL", raising=False)

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject must not be invoked on the send path"
        )

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    long_body = "a" * 80
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": long_body,
                "deliver_method": "store",
            },
        )
        msg_id = _latest_message_id_for(str(get_db_path()), "admin", "alice")
        row = _fetch_message_row(str(get_db_path()), msg_id)
        assert row is not None
        subject, parent_id, _content = row
        assert parent_id is None
        # NULL is the marker — the truncated body is NEVER stored.
        assert subject is None, f"expected NULL subject, got {subject!r}"


async def test_reply_keeps_subject_null_even_with_ollama(
    tmp_path, monkeypatch
) -> None:
    """parent_message_id set => subject is forced NULL on insert."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject must not run for replies — replies have NULL subject"
        )

    monkeypatch.setattr(message_suggestions, "suggest_subject", _boom)

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Root.
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "root body",
                "subject": "Root Topic",
                "deliver_method": "store",
            },
        )
        root_id = _latest_message_id_for(
            str(get_db_path()), "admin", "alice"
        )
        # Reply — note the explicit parent_message_id.
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "reply body",
                "parent_message_id": root_id,
                "deliver_method": "store",
            },
        )
        # Different message_id; fetch latest separately.
        # Pull the reply (the one whose parent matches root_id).
        conn = sqlite3.connect(str(get_db_path()))
        try:
            reply = conn.execute(
                "SELECT message_id, subject, parent_message_id "
                "FROM agent_messages WHERE parent_message_id = ?",
                (root_id,),
            ).fetchone()
        finally:
            conn.close()
        assert reply is not None, "reply row not found"
        _reply_id, subject, parent_id = reply
        assert subject is None, f"reply subject should be NULL, got {subject!r}"
        assert parent_id == root_id
