"""Threaded conversation view — repository + REST endpoint.

Feature 1 (message-threads-ui). ``MessageRepository.fetch_thread`` walks
``parent_message_id`` up to the ROOT of a message's thread, then collects
the WHOLE thread (all messages transitively descending from that root)
via a SQLite recursive CTE, projected through the canonical
``_message_to_dict`` and ordered oldest-first (root first).

The REST route ``GET /api/messages/{message_id}/thread`` exposes the same
under the operator-session gate.

Thread shape seeded by these tests (a tree — reply→reply chains plus a
sibling reply off the root)::

    root                (T0, subject "Topic")
     ├─ r1              (T1, reply to root)
     │   └─ r2          (T3, reply to r1)
     └─ sib             (T2, reply to root)

Oldest-first order is by timestamp: root, r1, sib, r2.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed_message(
    message_id: str,
    sender_id: str,
    recipient_id: str,
    content: str,
    timestamp: str,
    *,
    parent_message_id: str | None = None,
    subject: str | None = None,
) -> None:
    """INSERT one agent_messages row with explicit timestamp + threading.

    Direct SQL so the tests control timestamps (for the oldest-first
    ordering assertion) and the parent links precisely. Roots must be
    inserted before their replies (migration-0012 self-FK).
    """
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO agent_messages (message_id, sender_id, "
            "recipient_id, message_content, message_type, priority, "
            "timestamp, delivered, read, subject, parent_message_id) "
            "VALUES (?, ?, ?, ?, 'text', 'normal', ?, 0, 0, ?, ?)",
            (
                message_id, sender_id, recipient_id, content, timestamp,
                subject, parent_message_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tree() -> None:
    """Seed root ← r1 ← r2 plus a sibling reply (sib) off root."""
    _seed_message(
        "root", "admin", "alice", "root body",
        "2026-01-01T00:00:00", subject="Topic",
    )
    _seed_message(
        "r1", "alice", "admin", "reply one",
        "2026-01-01T00:01:00", parent_message_id="root",
    )
    _seed_message(
        "sib", "alice", "admin", "sibling reply",
        "2026-01-01T00:02:00", parent_message_id="root",
    )
    _seed_message(
        "r2", "admin", "alice", "reply two",
        "2026-01-01T00:03:00", parent_message_id="r1",
    )


# ---------- MessageRepository.fetch_thread ----------------------------


async def test_fetch_thread_from_leaf_returns_full_thread_oldest_first(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_tree()

        from agent_mcp.repositories import message_repo

        thread = message_repo.fetch_thread("r2")
        ids = [m["message_id"] for m in thread]
        # Full thread, ordered by timestamp ASC (root first).
        assert ids == ["root", "r1", "sib", "r2"], ids


async def test_fetch_thread_from_any_member_is_identical(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_tree()

        from agent_mcp.repositories import message_repo

        from_root = [m["message_id"] for m in message_repo.fetch_thread("root")]
        from_leaf = [m["message_id"] for m in message_repo.fetch_thread("r2")]
        from_sib = [m["message_id"] for m in message_repo.fetch_thread("sib")]
        assert from_root == from_leaf == from_sib == [
            "root", "r1", "sib", "r2",
        ]


async def test_fetch_thread_projects_message_to_dict_fields(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_tree()

        from agent_mcp.repositories import message_repo

        thread = message_repo.fetch_thread("root")
        root = thread[0]
        # Same projection shape as the canonical _message_to_dict.
        for key in (
            "message_id", "sender_id", "recipient_id", "message_content",
            "message_type", "priority", "timestamp", "delivered", "read",
            "subject", "subject_is_placeholder", "parent_message_id",
        ):
            assert key in root, f"missing {key} in {root}"
        assert root["subject"] == "Topic"
        # Replies carry no subject (thread-labelled instead).
        reply = next(m for m in thread if m["message_id"] == "r1")
        assert reply["parent_message_id"] == "root"
        assert reply["subject"] is None


async def test_fetch_thread_lone_root_is_single_element(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_message(
            "solo", "admin", "alice", "just me",
            "2026-01-01T00:00:00", subject="Alone",
        )

        from agent_mcp.repositories import message_repo

        thread = message_repo.fetch_thread("solo")
        assert [m["message_id"] for m in thread] == ["solo"]


async def test_fetch_thread_nonexistent_returns_empty(tmp_path) -> None:
    async with mcp_session(tmp_path):
        from agent_mcp.repositories import message_repo

        assert message_repo.fetch_thread("nope_does_not_exist") == []
        assert message_repo.fetch_thread("") == []


# ---------- GET /api/messages/{message_id}/thread ---------------------


async def test_rest_thread_endpoint_returns_ordered_thread(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_tree()

        r = admin.get("/api/messages/r1/thread")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "thread" in body, body
        ids = [m["message_id"] for m in body["thread"]]
        assert ids == ["root", "r1", "sib", "r2"], ids


async def test_rest_thread_endpoint_404_on_unknown_id(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.get("/api/messages/msg_doesnotexist/thread")
        assert r.status_code == 404, r.text


async def test_rest_thread_endpoint_rejects_bad_token(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        _seed_message(
            "solo", "admin", "alice", "hi", "2026-01-01T00:00:00",
        )
        r = admin.client.get(
            "/api/messages/solo/thread",
            headers={"Authorization": f"Bearer {'x' * 32}"},
        )
        assert r.status_code == 401, r.text


# ---------- reply implies read (mark parent read on reply) -------------


async def test_reply_marks_parent_message_read(tmp_path) -> None:
    """Replying to a message implies the sender has read it: the parent
    row flips to read=True. Feature: "if you reply to someone you must
    also have already read it." """
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        # Parent: bob -> alice, unread.
        _seed_message(
            "parent1", "bob", "alice", "ping",
            "2026-01-01T00:00:00", subject="Topic",
        )
        from agent_mcp.repositories import message_repo

        assert message_repo.get_by_id("parent1")["read"] is False, (
            "precondition: parent starts unread"
        )

        # alice replies to bob's message.
        await alice.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "bob",
                "message": "pong",
                "parent_message_id": "parent1",
            },
        )

        assert message_repo.get_by_id("parent1")["read"] is True, (
            "replying to a message must mark that parent message read"
        )


async def test_reply_from_non_recipient_does_not_mark_parent_read(
    tmp_path,
) -> None:
    """Defense-in-depth: the mark-read is scoped to the replier being the
    parent's recipient. A reply from anyone else must NOT clear the read
    flag — otherwise a third party could hide a message from the agent it
    was actually addressed to."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        await admin.create_worker("bob")
        carol = await admin.create_worker("carol")
        # Parent addressed to alice (NOT carol).
        _seed_message(
            "parent2", "bob", "alice", "ping",
            "2026-01-01T00:00:00",
        )

        # carol (not the recipient) replies referencing the parent.
        await carol.assert_tool_succeeds(
            "send_agent_message",
            {
                "recipient_id": "bob",
                "message": "butting in",
                "parent_message_id": "parent2",
            },
        )

        from agent_mcp.repositories import message_repo

        assert message_repo.get_by_id("parent2")["read"] is False, (
            "a reply from a non-recipient must not mark alice's message read"
        )
