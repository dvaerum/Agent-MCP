"""Tests for the hybrid event payload shapes (PR-2 event-coord).

Spec locked-decisions table:
  * `new_message` / `task_assigned` → **fat** payload (full row data).
  * `unassigned_task_appeared` → **skinny** (title + priority +
    required_capabilities; NO description).
  * `stop_listening` → minimal `{type, ref_id: None, timestamp,
    payload: {reason}}`.

Backwards-compat note: the existing event types `message`,
`broadcast`, `task_assigned`, `task_changed` already ship a fat
payload under the `data` key (per `_collect_events_for`). PR-2 adds
the new `unassigned_task_appeared` and `stop_listening` shapes
without changing the existing ones — clients that have learned the
old shape keep working.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def test_message_event_keeps_fat_data_shape(tmp_path: Path) -> None:
    """Regression: the existing `message` event's `data` blob must
    still carry the full row (sender_id, message_content, priority,
    timestamp, etc.). PR-2 must not strip these fields."""
    from tests.harness import mcp_session
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await send_agent_message_tool_impl({
            "token": admin.admin_token,
            "recipient_id": "alice",
            "message": "fat payload check",
            "deliver_method": "store",
        })

        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=2)
        ).isoformat()
        blocks = await alice.call(
            "wait_for_events",
            {"since": since, "timeout_seconds": 2},
        )
        body = json.loads(_content_text(blocks))
        messages = [
            e for e in body["events"] if e["type"] == "message"
        ]
        assert messages, f"no message event; got {body}"
        data = messages[0]["data"]
        # Fat shape: required keys for a useful client UX.
        for k in (
            "message_content", "sender_id", "recipient_id",
            "message_type", "priority", "timestamp",
        ):
            assert k in data, (
                f"fat message event missing key {k!r}; got {data}"
            )
        assert data["message_content"] == "fat payload check"


async def test_unassigned_task_event_is_skinny(tmp_path: Path) -> None:
    """`unassigned_task_appeared` payload must NOT include
    `description` (the spec's reason: workers call `view_task` if
    they're interested, keeping the wake event small)."""
    from tests.harness import mcp_session
    from agent_mcp.tools.task_tools import assign_task_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker")
        # Default empty capabilities — matches empty-required tasks
        # (which broadcasts to everyone).

        async def waiter():
            return await worker.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        await assign_task_tool_impl({
            "token": admin.admin_token,
            "task_title": "Skinny payload check",
            "task_description": (
                "This description should NOT appear in the wake event."
            ),
        })

        blocks = await asyncio.wait_for(w_task, timeout=5.0)
        body = json.loads(_content_text(blocks))
        unassigned = [
            e for e in body["events"]
            if e["type"] == "unassigned_task_appeared"
        ]
        assert unassigned, f"no unassigned_task_appeared; got {body}"
        payload = unassigned[0]["payload"]
        # Skinny invariants.
        assert "description" not in payload, (
            f"skinny payload must omit description; got {payload}"
        )
        # Required-positive invariants.
        for k in ("task_id", "title", "priority", "required_capabilities"):
            assert k in payload, (
                f"skinny payload missing key {k!r}; got {payload}"
            )
        # Envelope-level shape: ref_id + timestamp + type.
        for k in ("type", "ref_id", "timestamp", "payload"):
            assert k in unassigned[0], (
                f"envelope missing key {k!r}; got {unassigned[0]}"
            )


async def test_stop_listening_payload_shape(tmp_path: Path) -> None:
    """`stop_listening` event has `ref_id: None` and a `payload.reason`
    string."""
    from tests.harness import mcp_session
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # Flip the per-agent flag OFF so the next call returns
        # stop_listening immediately.
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agents SET auto_event_loop = 0 "
                "WHERE agent_id = ?",
                ("alice",),
            )
            conn.commit()
        finally:
            conn.close()

        blocks = await alice.call(
            "wait_for_events", {"timeout_seconds": 2}
        )
        body = json.loads(_content_text(blocks))
        assert len(body["events"]) == 1
        evt = body["events"][0]
        assert evt["type"] == "stop_listening"
        assert evt["ref_id"] is None
        assert isinstance(evt["timestamp"], str) and evt["timestamp"]
        assert isinstance(evt["payload"], dict)
        assert isinstance(evt["payload"]["reason"], str)
        assert evt["payload"]["reason"], "reason must be non-empty"
