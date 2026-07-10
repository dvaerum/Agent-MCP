"""BL-R20-1: the agent event feed must drain a >500-message backlog in
timestamp order across successive polls, losing nothing.

The bug: ``_collect_events_for`` fetched messages timestamp-DESC capped
at 500 (the newest 500), then reversed to present them ASC and advanced
the monotonic cursor to the MAX timestamp seen. When more than 500
messages had accrued since the cursor, only the NEWEST 500 were returned
and the cursor jumped PAST the dropped oldest tail — permanent
message-event loss on catch-up, and a control-message-burying /
censorship vector (flood 500+ messages right after a critical control
message so the critical one sits in the dropped-oldest tail).

The fix: fetch the OLDEST messages since the cursor first
(``timestamp ASC LIMIT 500``) so the returned batch is a contiguous
prefix starting at the cursor, and advance the cursor only to the max
timestamp of the RETURNED rows. A backlog then drains oldest-first over
successive polls with nothing skipped.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


_BASE = _dt.datetime(2026, 1, 1, 0, 0, 0)


def _ts(i: int) -> str:
    """A distinct, lexicographically-sortable ISO timestamp for row i."""
    return (_BASE + _dt.timedelta(seconds=i)).isoformat()


def _seed_messages(
    recipient: str,
    count: int,
    *,
    sender: str = "admin",
    start: int = 0,
    content_prefix: str = "msg",
) -> list[str]:
    """Bulk-insert ``count`` messages to ``recipient`` with strictly
    increasing timestamps. Returns the message_ids in timestamp order."""
    from agent_mcp.repositories.message_repository import (
        bulk_insert_messages,
    )

    rows = []
    ids = []
    for k in range(count):
        i = start + k
        mid = f"{content_prefix}-{i:05d}"
        ids.append(mid)
        rows.append({
            "message_id": mid,
            "sender_id": sender,
            "recipient_id": recipient,
            "message_content": f"{content_prefix} body {i}",
            "message_type": "info",
            "priority": "normal",
            "timestamp": _ts(i),
            "delivered": False,
            "read": False,
        })
    written = bulk_insert_messages(rows)
    assert written == count, f"seeded {written}, wanted {count}"
    return ids


def _message_ids(events: list[dict]) -> list[str]:
    return [
        e["data"]["message_id"]
        for e in events
        if e.get("type") in ("message", "broadcast")
    ]


async def test_backlog_over_500_drains_in_order_no_loss(
    tmp_path: Path,
) -> None:
    """Seed 600 messages, then drive ``_collect_events_for`` twice
    (advancing the cursor to max(returned) between polls, exactly as
    ``_envelope`` does). Across the two polls ALL 600 must be returned
    in timestamp order with NONE skipped.

    RED on main: the first poll returns the NEWEST 500 and the cursor
    jumps to the global max, so the oldest 100 are lost forever.
    GREEN after: first poll returns the oldest 500, cursor sits at the
    500th, second poll returns the remaining 100.
    """
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        _collect_events_for,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        expected = _seed_messages("alice", 600)

        since = _ts(-1)

        poll1 = _collect_events_for("alice", since)
        ids1 = _message_ids(poll1)
        assert len(ids1) == 500, (
            f"first poll should return the OLDEST 500 (contiguous "
            f"prefix), got {len(ids1)}"
        )
        assert ids1 == expected[:500], (
            "first poll must be the oldest 500 in timestamp order"
        )

        # Advance the cursor exactly as _envelope does: to max(returned).
        cursor1 = max(e["timestamp"] for e in poll1)
        assert cursor1 == _ts(499), (
            f"cursor must sit at the 500th oldest message, got {cursor1}"
        )

        poll2 = _collect_events_for("alice", cursor1)
        ids2 = _message_ids(poll2)
        assert ids2 == expected[500:], (
            f"second poll must return the remaining 100 in order, "
            f"got {len(ids2)} ids"
        )

        # No message may be skipped and none delivered twice.
        drained = ids1 + ids2
        assert drained == expected, (
            "the full backlog must drain in timestamp order across the "
            "two polls with nothing skipped or duplicated"
        )


async def test_critical_message_survives_flood_censorship(
    tmp_path: Path,
) -> None:
    """Censorship vector: a critical control message followed by a
    500+ flood must still be delivered. Oldest-first delivery puts the
    critical (oldest) message in the FIRST batch.

    RED on main: the critical message is the oldest, so it lands in the
    dropped-oldest tail and is never delivered.
    """
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        _collect_events_for,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")

        # The critical control message arrives first (oldest ts).
        critical_id = _seed_messages(
            "bob", 1, start=0, content_prefix="CRITICAL"
        )[0]
        # Attacker immediately floods 600 messages after it.
        _seed_messages("bob", 600, start=1, content_prefix="flood")

        since = _ts(-1)
        poll1 = _collect_events_for("bob", since)
        ids1 = _message_ids(poll1)

        assert critical_id in ids1, (
            "the critical control message (oldest) must be delivered in "
            "the first batch, not buried under the flood"
        )
        assert ids1[0] == critical_id, (
            "oldest-first delivery must surface the critical message "
            "at the head of the first batch"
        )


async def test_small_backlog_delivers_promptly_in_order(
    tmp_path: Path,
) -> None:
    """Regression: a normal small backlog still delivers in a single
    poll, in timestamp order."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        _collect_events_for,
    )

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("carol")
        expected = _seed_messages("carol", 3)

        poll = _collect_events_for("carol", _ts(-1))
        assert _message_ids(poll) == expected, (
            "a small backlog must deliver all messages in one poll, "
            "in timestamp order"
        )


async def test_message_repo_query_default_is_newest_first(
    tmp_path: Path,
) -> None:
    """Regression for other callers (message-list REST endpoint,
    agent-detail sample): ``query`` still defaults to timestamp-DESC
    (newest first); only the opt-in ``oldest_first=True`` flips it."""
    from tests.harness import mcp_session
    from agent_mcp.repositories import message_repo

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("dave")
        expected = _seed_messages("dave", 5)

        newest_first = message_repo.query({"to": "dave", "limit": 5})
        got_desc = [m["message_id"] for m in newest_first]
        assert got_desc == list(reversed(expected)), (
            "default query order must remain timestamp-DESC (newest "
            f"first) for existing callers, got {got_desc}"
        )

        oldest_first = message_repo.query(
            {"to": "dave", "limit": 5}, oldest_first=True
        )
        got_asc = [m["message_id"] for m in oldest_first]
        assert got_asc == expected, (
            "oldest_first=True must return timestamp-ASC, got "
            f"{got_asc}"
        )
