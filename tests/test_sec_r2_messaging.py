"""Round-2 messaging/event security regressions.

Three CONFIRMED business-logic findings, one test class each:

1. ``get_agent_messages`` marks the ENTIRE inbox read, ignoring the
   type / unread_only / limit predicate of the SELECT — so a filtered
   or paged fetch silently marks control messages the caller never saw
   as read (control-message loss). The mark-read must be scoped to the
   rows actually returned.

2. ``last_event_seen_at`` is written last-writer-wins, so a stale /
   lower cursor from a slow concurrent waiter can rewind the high-water
   mark and cause event replay. The write must be monotonic
   (``MAX(last_event_seen_at, ?)``, never regress).

3. A huge ``config_message_retention_days`` overflows ``timedelta`` and
   the retention sweep throws / stops. The value must be upper-clamped
   before constructing the timedelta.

Each test reproduces the bug on ``origin/main`` (RED) before the fix.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
from pathlib import Path

import pytest

from tests.harness import mcp_session, seed_agent_rows

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Shared DB helpers
# --------------------------------------------------------------------------


def _read_flag(message_id: str) -> int:
    """Return the persisted ``read`` flag for a message (authoritative)."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT read FROM agent_messages WHERE message_id = ?",
            (message_id,),
        )
        row = cur.fetchone()
        return int(row["read"]) if row is not None else -1
    finally:
        conn.close()


def _seed_message(
    sender: str,
    recipient: str,
    content: str,
    message_type: str,
    timestamp: str,
    read: int = 0,
) -> str:
    """Insert an unread message row directly, return its message_id."""
    from agent_mcp.db.connection import get_db_connection

    message_id = f"msg_{secrets.token_hex(8)}"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agent_messages (message_id, sender_id, "
            "recipient_id, message_content, message_type, priority, "
            "timestamp, delivered, read) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, sender, recipient, content, message_type,
             "normal", timestamp, 0, read),
        )
        conn.commit()
    finally:
        conn.close()
    return message_id


def _now_iso() -> str:
    return _dt.datetime.now().isoformat()


# --------------------------------------------------------------------------
# Finding 1 — mark-read must be scoped to the returned rows
# --------------------------------------------------------------------------


class TestMarkReadScope:
    async def test_type_filter_does_not_mark_other_types_read(
        self, tmp_path: Path
    ) -> None:
        """Fetching only 'text' must not mark a 'stop_command' read."""
        from agent_mcp.tools.agent_communication_tools import (
            get_agent_messages_tool_impl,
        )

        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice")

            ts = _now_iso()
            text_id = _seed_message("admin", "alice", "hi", "text", ts)
            ctrl_id = _seed_message(
                "admin", "alice", "STOP", "stop_command", ts
            )

            res = await get_agent_messages_tool_impl({
                "token": alice.token,
                "message_type": "text",
                "mark_as_read": True,
            })
            assert res.data["count"] == 1, res.message

            # The 'text' message we actually saw is now read.
            assert _read_flag(text_id) == 1
            # The control message we NEVER saw must stay unread.
            assert _read_flag(ctrl_id) == 0, (
                "control message was marked read despite being filtered "
                "out of the fetch — control-message loss"
            )

    async def test_limit_does_not_mark_beyond_page_read(
        self, tmp_path: Path
    ) -> None:
        """A limited page must not mark messages past the limit read."""
        from agent_mcp.tools.agent_communication_tools import (
            get_agent_messages_tool_impl,
        )

        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice")

            base = _dt.datetime.now()
            ids = []
            for i in range(3):
                ts = (base - _dt.timedelta(seconds=i)).isoformat()
                ids.append(
                    _seed_message("admin", "alice", f"m{i}", "text", ts)
                )

            res = await get_agent_messages_tool_impl({
                "token": alice.token,
                "limit": 1,
                "mark_as_read": True,
            })
            assert res.data["count"] == 1, res.message

            # Newest (ids[0]) is the single returned + marked-read row.
            read_flags = [_read_flag(m) for m in ids]
            marked = sum(read_flags)
            assert marked == 1, (
                f"expected exactly 1 row marked read (the page), got "
                f"{marked}: {read_flags}"
            )
            assert _read_flag(ids[0]) == 1

    async def test_returned_page_is_marked_read(
        self, tmp_path: Path
    ) -> None:
        """Positive: rows actually returned still get marked read."""
        from agent_mcp.tools.agent_communication_tools import (
            get_agent_messages_tool_impl,
        )

        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice")

            ts = _now_iso()
            a = _seed_message("admin", "alice", "a", "text", ts)
            b = _seed_message("admin", "alice", "b", "text", ts)

            res = await get_agent_messages_tool_impl({
                "token": alice.token,
                "mark_as_read": True,
            })
            assert res.data["count"] == 2, res.message
            assert _read_flag(a) == 1
            assert _read_flag(b) == 1

    async def test_sent_messages_are_not_marked_read(
        self, tmp_path: Path
    ) -> None:
        """A row the agent SENT (not received) must never be flipped by
        the recipient-side mark-read, even when include_sent surfaces
        it in the page."""
        from agent_mcp.tools.agent_communication_tools import (
            get_agent_messages_tool_impl,
        )

        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice")
            seed_agent_rows("bob")

            ts = _now_iso()
            sent_id = _seed_message("alice", "bob", "outbound", "text", ts)

            res = await get_agent_messages_tool_impl({
                "token": alice.token,
                "include_sent": True,
                "include_received": True,
                "mark_as_read": True,
            })
            assert res.data["count"] == 1, res.message
            # alice is the SENDER of sent_id, not the recipient — the
            # read flag belongs to bob's inbox view and must be left be.
            assert _read_flag(sent_id) == 0, (
                "a sent message was marked read on the sender's fetch"
            )


# --------------------------------------------------------------------------
# Finding 2 — last_event_seen_at must advance monotonically
# --------------------------------------------------------------------------


def _read_last_seen(agent_id: str) -> str | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_event_seen_at FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cur.fetchone()
        return row["last_event_seen_at"] if row else None
    finally:
        conn.close()


def _set_last_seen(agent_id: str, value: str | None) -> None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET last_event_seen_at = ? WHERE agent_id = ?",
            (value, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestMonotonicEventCursor:
    async def test_stale_cursor_does_not_regress(
        self, tmp_path: Path
    ) -> None:
        """A lower cursor value must NOT overwrite a newer one."""
        from agent_mcp.tools.agent_communication_tools import (
            _write_last_event_seen_at,
        )

        async with mcp_session(tmp_path):
            seed_agent_rows("alice")
            _set_last_seen("alice", "2026-06-01T00:00:00")

            # Slow waiter tries to write an OLDER cursor.
            _write_last_event_seen_at("alice", "2026-05-01T00:00:00")

            assert _read_last_seen("alice") == "2026-06-01T00:00:00", (
                "stale cursor rewound the high-water mark — event replay"
            )

    async def test_newer_cursor_advances(self, tmp_path: Path) -> None:
        """A higher cursor value advances the high-water mark."""
        from agent_mcp.tools.agent_communication_tools import (
            _write_last_event_seen_at,
        )

        async with mcp_session(tmp_path):
            seed_agent_rows("alice")
            _set_last_seen("alice", "2026-06-01T00:00:00")

            _write_last_event_seen_at("alice", "2026-07-01T00:00:00")

            assert _read_last_seen("alice") == "2026-07-01T00:00:00"

    async def test_first_write_when_null(self, tmp_path: Path) -> None:
        """The first write (column still NULL) must land, not be
        swallowed by MAX(NULL, ?) → NULL."""
        from agent_mcp.tools.agent_communication_tools import (
            _write_last_event_seen_at,
        )

        async with mcp_session(tmp_path):
            seed_agent_rows("alice")
            assert _read_last_seen("alice") is None

            _write_last_event_seen_at("alice", "2026-06-01T00:00:00")

            assert _read_last_seen("alice") == "2026-06-01T00:00:00"


# --------------------------------------------------------------------------
# Finding 3 — retention days must be upper-clamped before timedelta
# --------------------------------------------------------------------------


def _set_retention_days(days: int) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = _now_iso()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO project_context "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("config_message_retention_days", json.dumps(days),
             "retention test", now, "test", now, "test"),
        )
        conn.commit()
    finally:
        conn.close()


class TestRetentionClamp:
    async def test_huge_retention_does_not_overflow(
        self, tmp_path: Path
    ) -> None:
        """A huge retention value must not throw OverflowError; the
        sweep still runs (clamped window)."""
        from agent_mcp.features.message_retention import prune_old_messages

        async with mcp_session(tmp_path):
            seed_agent_rows("alice")
            # Older than any sane clamp (~10y) so it's deletable once
            # the window is clamped.
            old_ts = (
                _dt.datetime.now() - _dt.timedelta(days=4000)
            ).isoformat()
            old_id = _seed_message(
                "admin", "alice", "ancient", "text", old_ts, read=1
            )

            _set_retention_days(10 ** 18)

            # Must not raise; must actually prune the ancient row.
            deleted = prune_old_messages()
            assert deleted == 1, f"expected 1 pruned, got {deleted}"
            assert _read_flag(old_id) == -1, "ancient row should be gone"

    async def test_clamped_window_still_keeps_recent(
        self, tmp_path: Path
    ) -> None:
        """With the huge (clamped) window, a recent read message stays —
        the clamp bounds the window, it doesn't delete everything."""
        from agent_mcp.features.message_retention import prune_old_messages

        async with mcp_session(tmp_path):
            seed_agent_rows("alice")
            recent_ts = (
                _dt.datetime.now() - _dt.timedelta(days=5)
            ).isoformat()
            recent_id = _seed_message(
                "admin", "alice", "recent", "text", recent_ts, read=1
            )

            _set_retention_days(10 ** 18)
            prune_old_messages()

            assert _read_flag(recent_id) == 1, (
                "recent read message deleted — clamp too aggressive"
            )
