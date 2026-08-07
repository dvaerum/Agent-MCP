"""Tests for the event payload shapes.

Events are POINTERS, not dumps — each says "something changed for you"
so the agent decides whether to fetch it (and that fetch is what marks a
message read / makes it interact with a task):
  * `message` / `broadcast` → **skinny**: `{message_id, sender_id,
    subject, is_reply, priority}` — NO `message_content`. An untitled
    root is held for the async AI subject backfill when subject-gen is
    ON (fires with the 50-char preview when OFF or after a max-hold).
  * `task_assigned` / `task_changed` / `unassigned_task_appeared` →
    **skinny**: `{task_id, title, status, priority}` — NO description.
  * `directive` → the ONE inline exception: `data.prompt` is the command.
  * `stop_listening` → minimal `{type, ref_id: None, timestamp,
    payload: {reason}}`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest

from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def test_message_event_is_skinny(tmp_path: Path) -> None:
    """A `message` event is a POINTER, not a dump: it carries the
    subject/title + is_reply + sender, but NOT the full message_content.
    The agent calls get_agent_messages to READ the body — which is what
    marks the message read. (Subject-gen is OFF in tests, so an untitled
    message fires immediately with the 50-char preview as its subject.)"""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl({
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "the full body that must NOT be dumped in the event",
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
        # Skinny: the body is NOT in the event — it's a pointer.
        assert "message_content" not in data, (
            f"message event must not dump the body; got {data}"
        )
        # Pointer fields the agent needs to decide + fetch.
        for k in ("message_id", "sender_id", "subject", "is_reply"):
            assert k in data, (
                f"skinny message event missing key {k!r}; got {data}"
            )
        assert data["subject"], "subject/title should be present (preview)"
        assert data["is_reply"] is False


async def test_task_assigned_event_is_skinny(tmp_path: Path) -> None:
    """`task_assigned` / `task_changed` events are pointers too: task_id
    + title + status, NOT the full description. The agent calls
    view_tasks to read + interact."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker")

        async def waiter():
            return await worker.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        with with_bearer(admin.admin_token):
            await assign_task_tool_impl({
                "token": admin.admin_token,
                "agent_id": "worker",
                "task_title": "Skinny task title",
                "task_description": (
                    "This long description should NOT appear in the event."
                ),
            })

        blocks = await asyncio.wait_for(w_task, timeout=5.0)
        body = json.loads(_content_text(blocks))
        tasks = [
            e for e in body["events"]
            if e["type"] in ("task_assigned", "task_changed")
        ]
        assert tasks, f"no task event; got {body}"
        data = tasks[0]["data"]
        assert "description" not in data, (
            f"task event must omit description; got {data}"
        )
        for k in ("task_id", "title", "status"):
            assert k in data, (
                f"skinny task event missing key {k!r}; got {data}"
            )


async def test_unassigned_task_event_is_skinny(tmp_path: Path) -> None:
    """`unassigned_task_appeared` payload must NOT include
    `description` (the spec's reason: workers call `view_tasks` if
    they're interested, keeping the wake event small)."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("worker")
        # Every unassigned task now broadcasts to every active agent
        # (capability-tag routing retired in PR5).

        async def waiter():
            return await worker.call(
                "wait_for_events", {"timeout_seconds": 3}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        with with_bearer(admin.admin_token):
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
        for k in ("task_id", "title", "priority"):
            assert k in payload, (
                f"skinny payload missing key {k!r}; got {payload}"
            )
        # Capability-tag routing was retired: no required_capabilities key.
        assert "required_capabilities" not in payload, (
            f"required_capabilities must be gone from payload; got {payload}"
        )
        # Envelope-level shape: ref_id + timestamp + type.
        for k in ("type", "ref_id", "timestamp", "payload"):
            assert k in unassigned[0], (
                f"envelope missing key {k!r}; got {unassigned[0]}"
            )


async def test_stop_listening_payload_shape(tmp_path: Path) -> None:
    """`stop_listening` event has `ref_id: None` and a `payload.reason`
    string."""
    from agent_mcp.db.connection import get_db_connection
    from tests.harness import mcp_session

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


async def test_untitled_message_held_until_titled_when_gen_on(
    tmp_path: Path, monkeypatch,
) -> None:
    """When AI subject-gen is ON, an untitled root message's skinny event
    is HELD (not delivered) until it gets a real subject — so the pointer
    always carries a proper title. Titling it releases the event."""
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "test-model")
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import message_repo
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl({
                "token": admin.admin_token,
                "recipient_id": "alice",
                "message": "untitled body waiting on an AI subject",
                "deliver_method": "store",
            })

        since = (_dt.datetime.now() - _dt.timedelta(seconds=2)).isoformat()
        # HELD: no message event while untitled + gen ON.
        blocks = await alice.call(
            "wait_for_events", {"since": since, "timeout_seconds": 2}
        )
        body = json.loads(_content_text(blocks))
        assert not [e for e in body["events"] if e["type"] == "message"], (
            f"untitled message must be held when gen ON; got {body}"
        )

        # Simulate the backfill titling it.
        conn = get_db_connection()
        try:
            mid = conn.execute(
                "SELECT message_id FROM agent_messages WHERE recipient_id='alice'"
            ).fetchone()[0]
        finally:
            conn.close()
        message_repo.set_message_subject(mid, "Generated Subject")

        # RELEASED: now the skinny event fires with the real title.
        blocks2 = await alice.call(
            "wait_for_events", {"since": since, "timeout_seconds": 2}
        )
        body2 = json.loads(_content_text(blocks2))
        msgs = [e for e in body2["events"] if e["type"] == "message"]
        assert msgs, f"titled message should now fire; got {body2}"
        assert msgs[0]["data"]["subject"] == "Generated Subject"
        assert "message_content" not in msgs[0]["data"]
