"""Tools block: `send_agent_message` accepts subject + parent + auto-fills.

RED test for behavior block 2 (v5.0.22): the MCP tool layer learns
about the two new agent_messages columns. Specifically:

* `send_agent_message` gains optional `subject` and
  `parent_message_id` arguments.
* When the caller passes `subject`, it is persisted verbatim
  (no LLM call).
* When the caller omits `subject` for a root message
  (`parent_message_id` absent / None) and
  `AGENT_MCP_SUBJECT_MODEL` is set, the implementation calls
  `agent_mcp.features.message_suggestions.suggest_subject(content)`
  and stores the returned string.
* When the caller omits `subject` for a root message and
  `AGENT_MCP_SUBJECT_MODEL` is unset, the implementation falls back
  to `content[:50] + "..."` — no LLM call.
* When the caller is sending a reply
  (`parent_message_id` is set), `subject` stays NULL regardless of
  the Ollama config — replies don't carry subjects.

Ollama is mocked at the helper level (`message_suggestions.suggest_subject`)
rather than at the HTTP layer — cheaper and the harness already
intercepts httpx for embeddings.
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


async def test_root_without_subject_uses_ollama_helper(
    tmp_path, monkeypatch
) -> None:
    """Root message + no subject + Ollama configured = helper fills it."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    called = {"count": 0, "content": None}

    async def _mock_suggest(content: str) -> str | None:
        called["count"] += 1
        called["content"] = content
        return "Mocked Subject"

    monkeypatch.setattr(message_suggestions, "suggest_subject", _mock_suggest)

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
        assert subject == "Mocked Subject", subject
        assert parent_id is None
        assert called["count"] == 1
        assert called["content"] == "please help with the build"


async def test_root_without_subject_no_ollama_falls_back(
    tmp_path, monkeypatch
) -> None:
    """Root + no subject + AGENT_MCP_SUBJECT_MODEL unset = truncated body fallback."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    monkeypatch.delenv("AGENT_MCP_SUBJECT_MODEL", raising=False)

    # If the helper were called we'd fail loudly.
    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject must not be invoked when "
            "AGENT_MCP_SUBJECT_MODEL is unset"
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
        # Fallback rule: content[:50] + "..." for any root without an
        # explicit subject when no Ollama backend is configured.
        assert subject == long_body[:50] + "...", subject


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
