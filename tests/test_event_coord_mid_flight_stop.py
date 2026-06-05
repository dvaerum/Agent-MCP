"""Tests for mid-flight `stop_listening` on toggle flip.

Spec (PR-2 event-coord): when an operator flips the per-agent or
global toggle OFF while a `wait_for_events` call is blocked, the
call must wake within ~5s with a `stop_listening` envelope.
Implementation: the toggle-write code wakes affected waiters via
`signal_for(agent_id).set()`, and the impl re-evaluates flags on
wake before draining events.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _content_text(blocks) -> str:
    assert blocks, "tool returned no content blocks"
    return blocks[0].text


async def test_per_agent_toggle_off_wakes_in_flight(tmp_path: Path) -> None:
    """A `wait_for_events` already in flight returns within 5s with
    `stop_listening` when the per-agent flag is flipped OFF (test
    case 8 of the VM E2E plan)."""
    from tests.harness import mcp_session
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        async def waiter():
            return await alice.call(
                "wait_for_events", {"timeout_seconds": 60}
            )

        w_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.3)

        start = asyncio.get_event_loop().time()
        # Simulate the dashboard PATCH path: flip the column + wake
        # the waiter (same two-step the routes endpoint performs).
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
        g.wake_for_flag_recheck("alice")

        blocks = await asyncio.wait_for(w_task, timeout=10.0)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 5.0, (
            f"in-flight wait should return within ~5s of flag flip; "
            f"took {elapsed:.2f}s"
        )
        body = json.loads(_content_text(blocks))
        assert len(body["events"]) == 1
        assert body["events"][0]["type"] == "stop_listening"


async def test_global_toggle_off_wakes_all_in_flight(tmp_path: Path) -> None:
    """A global toggle flip wakes EVERY in-flight wait — verified
    with two agents simultaneously waiting."""
    from tests.harness import mcp_session
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection
    import datetime as _dt

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")

        async def waiter(session):
            return await session.call(
                "wait_for_events", {"timeout_seconds": 60}
            )

        a_task = asyncio.create_task(waiter(alice))
        b_task = asyncio.create_task(waiter(bob))
        await asyncio.sleep(0.3)

        start = asyncio.get_event_loop().time()
        # Set the global flag OFF + wake everyone.
        now = _dt.datetime.now().isoformat()
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT context_key FROM project_context "
                "WHERE context_key = ?",
                ("config_auto_event_loop_global",),
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE project_context SET value = 'false', "
                    "updated_at = ? WHERE context_key = ?",
                    (now, "config_auto_event_loop_global"),
                )
            else:
                cur.execute(
                    "INSERT INTO project_context "
                    "(context_key, value, created_at, updated_at, "
                    " created_by, updated_by) "
                    "VALUES (?, 'false', ?, ?, 'admin', 'admin')",
                    ("config_auto_event_loop_global", now, now),
                )
            conn.commit()
        finally:
            conn.close()
        g.wake_all_for_flag_recheck()

        a_blocks, b_blocks = await asyncio.gather(
            asyncio.wait_for(a_task, timeout=10.0),
            asyncio.wait_for(b_task, timeout=10.0),
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 5.0, (
            f"global flip should wake all within ~5s; "
            f"took {elapsed:.2f}s"
        )
        for who, blocks in (("alice", a_blocks), ("bob", b_blocks)):
            body = json.loads(_content_text(blocks))
            assert len(body["events"]) == 1, (
                f"{who} should get 1 event; got {body}"
            )
            assert body["events"][0]["type"] == "stop_listening", (
                f"{who} should get stop_listening; got {body}"
            )
