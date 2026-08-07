"""Tests for the wait_for_event + inbox/status helpers added to
`tests/harness.py` (plan Phase 5).

The harness should make event-driven assertions trivial — a test
writes one line, not five — so future Phase 7 daemon-loop tests
and similar end-to-end work isn't blocked on plumbing.

Helpers under test:

* `WorkerSession.wait_for_event(since=None, timeout=5)` —
  thin wrapper around `tools/call wait_for_events` that returns
  the parsed envelope dict.
* `WorkerSession.read_inbox()` — `resources/read` on the
  session's `agent-mcp://inbox/<agent_id>` URI, returns parsed
  JSON envelope.
* `WorkerSession.read_status()` — `resources/read` on the
  session's `agent-mcp://status/<agent_id>` URI, returns parsed
  counter dict.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest

from tests.harness import with_bearer

pytestmark = pytest.mark.asyncio


async def test_wait_for_event_helper_returns_envelope(
    tmp_path: Path,
) -> None:
    """`wait_for_event` returns a parsed envelope dict
    (`{events: [...], next_cursor: "..."}`), not raw TextContent."""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = (
            _dt.datetime.now() - _dt.timedelta(seconds=1)
        ).isoformat()

        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "via helper",
                    "deliver_method": "store",
                }
            )

        env = await alice.wait_for_event(since=since, timeout=2)
        assert "events" in env, f"helper must return envelope; got {env!r}"
        assert len(env["events"]) == 1
        # Skinny message event: the body isn't dumped — an untitled root
        # surfaces via its subject preview (see wait_for_events).
        assert "via helper" in (env["events"][0]["data"].get("subject") or "")


async def test_wait_for_event_helper_wakes_within_one_second(
    tmp_path: Path,
) -> None:
    """The helper exposes the same wake-on-event semantics as the
    raw tool call — assert a concurrent waiter wakes when a message
    fires."""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()

        async def waiter():
            return await alice.wait_for_event(since=since, timeout=10)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)

        start = asyncio.get_event_loop().time()
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "wake-helper",
                    "deliver_method": "store",
                }
            )
        env = await asyncio.wait_for(task, timeout=5.0)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 2.0
        assert "wake-helper" in (env["events"][0]["data"].get("subject") or "")


async def test_read_inbox_helper(tmp_path: Path) -> None:
    """`read_inbox()` reads the calling agent's inbox resource and
    returns the parsed envelope dict."""
    from agent_mcp.tools.agent_communication_tools import (
        send_agent_message_tool_impl,
    )
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        with with_bearer(admin.admin_token):
            await send_agent_message_tool_impl(
                {
                    "token": admin.admin_token,
                    "recipient_id": "alice",
                    "message": "for inbox",
                    "deliver_method": "store",
                }
            )
        env = await alice.read_inbox()
        assert env.get("events"), f"missing events; got {env!r}"
        assert "for inbox" in (env["events"][0]["data"].get("subject") or "")


async def test_read_status_helper(tmp_path: Path) -> None:
    """`read_status()` reads the status resource and returns the
    counter dict."""
    from agent_mcp.tools.task_tools import assign_task_tool_impl
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # Pre-state: no tasks.
        s0 = await alice.read_status()
        assert s0["unfinished_tasks"] == 0
        assert s0["unread_messages"] == 0

        with with_bearer(admin.admin_token):
            await assign_task_tool_impl(
                {
                    "token": admin.admin_token,
                    "agent_token": alice.token,
                    "task_title": "Brew tea",
                    "task_description": "Two minutes steep.",
                    "priority": "low",
                }
            )

        s1 = await alice.read_status()
        assert s1["unfinished_tasks"] == 1, f"got {s1!r}"
