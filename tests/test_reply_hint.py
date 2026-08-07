"""Feature 2 — nudge agents off "RE:" subjects toward parent_message_id.

When an agent types an explicit subject that looks like a reply
(``^\\s*re\\s*:``) but does NOT set ``parent_message_id``, the MCP
``send_agent_message`` tool still SUCCEEDS (advisory only), and appends a
gentle hint — both a ``reply_hint`` field on ``Ok.data`` and a sentence
on the human ``message`` — steering the agent to the reply/threading
function (``parent_message_id``) next time.

No hint fires when:
  * the message is a real reply (``parent_message_id`` set), or
  * the subject is a normal (non-"RE:") subject.

RE: only — there is no forward/"FW:" concept in this product.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _joined_text(blocks) -> str:
    parts = []
    for b in blocks:
        t = getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


async def test_re_subject_without_parent_gets_reply_hint(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        blocks = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "here is my reply body",
                "subject": "RE: the build failure",
                "deliver_method": "store",
            },
        )
        text = _joined_text(blocks)
        # The data block carries the structured hint field.
        assert "reply_hint" in text, text
        # The human message nudges toward parent_message_id.
        assert "parent_message_id" in text, text


async def test_re_subject_case_and_whitespace_insensitive(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        for subj in ("re: lowercase", "  Re :  spaced", "RE:tight"):
            blocks = await admin.assert_tool_succeeds(
                "send_agent_message",
                {
                    "recipient_id": "alice",
                    "message": "body",
                    "subject": subj,
                    "deliver_method": "store",
                },
            )
            assert "reply_hint" in _joined_text(blocks), subj


async def test_real_reply_gets_no_hint(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # Root.
        await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "root body",
                "subject": "Topic",
                "deliver_method": "store",
            },
        )
        import sqlite3

        from agent_mcp.core.config import get_db_path

        conn = sqlite3.connect(str(get_db_path()))
        try:
            root_id = conn.execute(
                "SELECT message_id FROM agent_messages "
                "WHERE sender_id='admin' AND recipient_id='alice' "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()

        # A proper reply — parent set. Even a subject won't be present
        # (replies force subject NULL), and no hint should appear.
        blocks = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "actual reply",
                "parent_message_id": root_id,
                "deliver_method": "store",
            },
        )
        assert "reply_hint" not in _joined_text(blocks)


async def test_normal_subject_gets_no_hint(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        blocks = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "regular message",
                "subject": "Deployment status",
                "deliver_method": "store",
            },
        )
        assert "reply_hint" not in _joined_text(blocks)


async def test_re_lookalike_word_does_not_trigger(tmp_path) -> None:
    """A subject that merely starts with 'Re' as part of a word (e.g.
    'Reminder') is not a reply marker — the regex requires 're' + ':'."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        blocks = await admin.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "alice",
                "message": "body",
                "subject": "Reminder: standup at 10",
                "deliver_method": "store",
            },
        )
        assert "reply_hint" not in _joined_text(blocks)
