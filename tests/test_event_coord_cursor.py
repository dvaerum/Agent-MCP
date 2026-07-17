"""Tests for the cursor (catch-up) behavior of `fetch_events_since`
and `wait_for_events` cursor persistence.

Spec (PR-2 event-coord):
  * `fetch_events_since(cursor: str | None) -> {events, cursor}` is
    a pure DB query, no blocking.
  * If `cursor=None`, fetches from `agents.last_event_seen_at`.
  * Both `wait_for_events` and `fetch_events_since` write
    `agents.last_event_seen_at = max(timestamps)` after returning
    events.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


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


async def test_fetch_events_since_is_registered(tmp_path: Path) -> None:
    """The new tool appears in `tools/list` for any agent."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        names = {t.name for t in await admin.list_tools()}
        assert "fetch_events_since" in names, (
            f"fetch_events_since missing from tools/list; got {sorted(names)}"
        )


async def test_fetch_events_since_returns_messages(tmp_path: Path) -> None:
    """`fetch_events_since(None)` returns messages persisted while
    the agent was offline."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl({
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "you missed this",
                "deliver_method": "store",
            })

        blocks = await alice.call("fetch_events_since", {})
        body = json.loads(_content_text(blocks))
        assert body.get("events"), f"want events; got {body}"
        # cursor should advance past the message timestamp.
        assert body.get("cursor"), f"want non-empty cursor; got {body}"

        # Second call with returned cursor → no new events.
        blocks2 = await alice.call(
            "fetch_events_since", {"cursor": body["cursor"]}
        )
        body2 = json.loads(_content_text(blocks2))
        assert body2.get("events") == [], (
            f"second call should be empty; got {body2}"
        )


async def test_wait_for_events_persists_last_seen_at(
    tmp_path: Path,
) -> None:
    """After `wait_for_events` returns events,
    `agents.last_event_seen_at` is updated to the high-water
    timestamp."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl({
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "hi",
                "deliver_method": "store",
            })

        assert _read_last_seen("alice") is None, (
            "precondition: no cursor persisted yet"
        )

        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=2)
        ).isoformat()
        blocks = await alice.call(
            "wait_for_events",
            {"since": since, "timeout_seconds": 2},
        )
        body = json.loads(_content_text(blocks))
        assert body.get("events"), f"want events; got {body}"

        persisted = _read_last_seen("alice")
        assert persisted, (
            f"last_event_seen_at should be persisted; "
            f"db value={persisted}"
        )


async def test_fetch_events_since_null_cursor_uses_persisted(
    tmp_path: Path,
) -> None:
    """`fetch_events_since` with cursor=None falls back to the
    persisted `last_event_seen_at`. Verified by pre-seeding the
    column with a far-future timestamp — the call should return no
    events even though we just persisted one."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl({
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "old news",
                "deliver_method": "store",
            })

        # Pin the cursor far in the future so the message is "older".
        far_future = "9999-01-01T00:00:00"
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agents SET last_event_seen_at = ? WHERE agent_id = ?",
                (far_future, "alice"),
            )
            conn.commit()
        finally:
            conn.close()

        blocks = await alice.call("fetch_events_since", {})
        body = json.loads(_content_text(blocks))
        assert body.get("events") == [], (
            f"cursor=None should use persisted future ts; got {body}"
        )
