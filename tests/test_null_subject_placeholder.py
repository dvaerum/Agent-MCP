"""Phase 1: null-subject placeholder.

Locked behaviour:

* **Send path never calls the model.** A root message sent WITHOUT an
  explicit ``subject`` stores ``subject = NULL`` — no synchronous
  ``suggest_subject`` call, no ``content[:50]`` text persisted. NULL is
  the marker meaning "no real subject was ever set". An explicit subject
  is stored verbatim (non-null).
* **Read paths compute a 50-char preview when the stored subject is
  NULL** and expose a ``subject_is_placeholder`` boolean — both on the
  MCP ``get_agent_messages`` tool AND the REST ``/api/messages/query``
  path. A real subject reads back verbatim with the flag ``false``.
* Bodies ≤50 chars → preview has no trailing ``"..."``; bodies >50 chars
  → preview ends with ``"..."``.

The ``suggest_subject`` helper and ``/api/messages/suggest-subject``
endpoint stay intact (reused by the dashboard button + the Phase 2
backfill) — this phase only removes the synchronous call at SEND time.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# --- direct DB helpers -------------------------------------------------------


def _fetch_subject(db_path: str, message_id: str) -> tuple:
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


def _messages_payload(content_blocks) -> list[dict]:
    """Decode the structured `messages` list from a get_agent_messages
    tool result. The renderer emits two text blocks — a prose summary
    first, the JSON `data` payload second — so parse the last JSON block.
    """
    for block in reversed(content_blocks):
        text = getattr(block, "text", "") or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "messages" in data:
            return data["messages"]
    raise AssertionError("no JSON messages payload in tool result")


# --- unit: the shared helper -------------------------------------------------


async def test_message_subject_view_helper_short_and_long() -> None:
    from agent_mcp.repositories.message_repository import message_subject_view

    # Real subject → verbatim, not a placeholder.
    assert message_subject_view("Real Topic", "body") == ("Real Topic", False)

    # NULL subject, short body (≤50) → preview, no ellipsis.
    short = "a" * 50
    assert message_subject_view(None, short) == (short, True)

    # NULL subject, long body (>50) → 50-char preview + ellipsis.
    long_body = "b" * 80
    disp, is_ph = message_subject_view(None, long_body)
    assert disp == "b" * 50 + "..."
    assert is_ph is True

    # Empty subject string treated as NULL.
    assert message_subject_view("", "hello")[1] is True


# --- send path: NULL stored, model never called ------------------------------


async def test_root_without_subject_stores_null_and_skips_model(
    tmp_path, monkeypatch
) -> None:
    """Root + no subject → DB subject IS NULL, and suggest_subject is
    NEVER invoked even when AGENT_MCP_SUBJECT_MODEL is configured."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    # Even with a model configured, the send path must not touch it.
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    async def _boom(content: str) -> str | None:  # pragma: no cover
        raise AssertionError(
            "suggest_subject must NOT be called on the send path "
            f"(content={content!r})"
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
        subject, parent_id, _content = _fetch_subject(
            str(get_db_path()), msg_id
        )
        assert subject is None, f"expected NULL subject, got {subject!r}"
        assert parent_id is None


async def test_explicit_subject_stored_nonnull(tmp_path, monkeypatch) -> None:
    """Explicit subject → stored verbatim, no model call."""
    from agent_mcp.core.config import get_db_path
    from agent_mcp.features import message_suggestions

    def _boom(content: str):  # pragma: no cover
        raise AssertionError("suggest_subject must not run for explicit subject")

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
        subject, parent_id, _content = _fetch_subject(
            str(get_db_path()), msg_id
        )
        assert subject == "Custom Topic"
        assert parent_id is None


# --- MCP read path: get_agent_messages ---------------------------------------


async def test_mcp_read_null_subject_gets_preview_and_flag(tmp_path) -> None:
    """Reading a null-subject root back via get_agent_messages yields the
    50-char body preview + subject_is_placeholder == True."""
    long_body = "z" * 80
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": long_body,
                "deliver_method": "store",
            },
        )
        blocks = await alice.assert_tool_succeeds(
            "get_agent_messages", {"include_received": True}
        )
        messages = _messages_payload(blocks)
        assert len(messages) == 1, messages
        row = messages[0]
        assert row["subject"] == "z" * 50 + "...", row
        assert row["subject_is_placeholder"] is True, row


async def test_mcp_read_real_subject_flag_false(tmp_path) -> None:
    """A real subject reads back verbatim with the flag false."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "the body",
                "subject": "Real Subject",
                "deliver_method": "store",
            },
        )
        blocks = await alice.assert_tool_succeeds(
            "get_agent_messages", {"include_received": True}
        )
        row = _messages_payload(blocks)[0]
        assert row["subject"] == "Real Subject", row
        assert row["subject_is_placeholder"] is False, row


async def test_mcp_read_short_body_preview_no_ellipsis(tmp_path) -> None:
    """A ≤50-char body yields a preview with no trailing '...'."""
    short_body = "under fifty chars"
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": short_body,
                "deliver_method": "store",
            },
        )
        blocks = await alice.assert_tool_succeeds(
            "get_agent_messages", {"include_received": True}
        )
        row = _messages_payload(blocks)[0]
        assert row["subject"] == short_body, row
        assert not row["subject"].endswith("..."), row
        assert row["subject_is_placeholder"] is True, row


# --- REST read path: POST /api/messages/query --------------------------------


async def test_rest_query_null_subject_gets_preview_and_flag(
    tmp_path,
) -> None:
    """A root sent via REST with no subject reads back through
    /api/messages/query with a body preview + placeholder flag."""
    long_body = "q" * 80
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={"recipient_id": "alice", "message_content": long_body},
        )
        assert r.status_code == 200, r.text
        msg_id = r.json()["message_id"]

        q = admin.post("/api/messages/query", json={})
        assert q.status_code == 200, q.text
        rows = {row["message_id"]: row for row in q.json()["messages"]}
        row = rows[msg_id]
        assert row["subject"] == "q" * 50 + "...", row
        assert row["subject_is_placeholder"] is True, row


async def test_rest_query_real_subject_flag_false(tmp_path) -> None:
    """A REST-composed message with an explicit subject reads back
    verbatim with the flag false."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "body text",
                "subject": "Chosen Subject",
            },
        )
        assert r.status_code == 200, r.text
        msg_id = r.json()["message_id"]

        q = admin.post("/api/messages/query", json={})
        rows = {row["message_id"]: row for row in q.json()["messages"]}
        row = rows[msg_id]
        assert row["subject"] == "Chosen Subject", row
        assert row["subject_is_placeholder"] is False, row
