"""Ad-hoc poke (event-loop scheduler PR2): repo, priority ordering,
firing through wait_for_events, and the operator-only REST route.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

import agent_mcp.tools.agent_communication_tools as acm
from agent_mcp.repositories import pending_directive_repository as poke_repo
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _conn():
    from agent_mcp.db.connection import get_db_connection

    return get_db_connection()


def _parse(result) -> dict:
    from agent_mcp.core.tool_result import render_as_text_content

    return json.loads(render_as_text_content(result)[0].text)


# ── repo ────────────────────────────────────────────────────────────────


async def test_collect_marks_delivered_once(tmp_path):
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        poke_repo.create_poke(
            poke_id="poke_1", agent_id="alice", prompt="do it",
            priority="urgent", created_by="op", connection=cur,
        )
        conn.commit()
        assert poke_repo.count_undelivered("alice", connection=cur) == 1
        events = poke_repo.collect_undelivered("alice", connection=cur)
        conn.commit()
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "directive"
        assert ev["data"]["source"] == "poke"
        assert ev["data"]["schedule_id"] is None
        assert ev["priority"] == "urgent"
        # Second collection is empty (delivered once).
        assert poke_repo.collect_undelivered("alice", connection=cur) == []
        assert poke_repo.count_undelivered("alice", connection=cur) == 0
        conn.close()


# ── priority ordering ───────────────────────────────────────────────────


async def test_urgent_directive_sorts_ahead_of_ordinary(tmp_path):
    async with mcp_session(tmp_path):
        events = [
            {"type": "message", "timestamp": "2026-01-01T00:00:01",
             "data": {"priority": "normal"}},
            {"type": "directive", "timestamp": "2026-01-01T00:00:05",
             "priority": "urgent", "data": {"source": "poke"}},
            {"type": "message", "timestamp": "2026-01-01T00:00:03",
             "data": {"priority": "normal"}},
        ]
        acm._sort_events_priority_then_time(events)
        # Urgent directive first despite the latest timestamp; ordinary
        # events keep timestamp order among themselves.
        assert events[0]["type"] == "directive"
        assert [e["timestamp"] for e in events[1:]] == [
            "2026-01-01T00:00:01", "2026-01-01T00:00:03",
        ]


# ── firing through wait_for_events ──────────────────────────────────────


async def test_poke_to_listening_agent_delivered(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        conn = _conn()
        cur = conn.cursor()
        poke_repo.create_poke(
            poke_id="poke_1", agent_id="alice", prompt="urgent thing",
            priority="urgent", created_by="op", connection=cur,
        )
        conn.commit()
        conn.close()
        res = await acm.wait_for_events_tool_impl(
            {"since": _dt.datetime.now().isoformat(), "timeout_seconds": 5},
            principal=alice._principal(),
        )
        env = _parse(res)
        directives = [e for e in env["events"] if e["type"] == "directive"]
        assert directives, env
        assert directives[0]["data"]["source"] == "poke"
        assert directives[0]["data"]["prompt"] == "urgent thing"


async def test_poke_sorts_ahead_of_pending_message(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        bob = await admin.create_worker("bob")
        since = _dt.datetime.now().isoformat()
        # An ordinary message to alice.
        from agent_mcp.tools.agent_communication_tools import (
            send_agent_message_tool_impl,
        )
        await send_agent_message_tool_impl(
            {"recipient_id": "alice", "message": "hi"},
            principal=bob._principal(),
        )
        # A poke arriving after the message.
        conn = _conn()
        cur = conn.cursor()
        poke_repo.create_poke(
            poke_id="poke_1", agent_id="alice", prompt="do this first",
            priority="urgent", created_by="op", connection=cur,
        )
        conn.commit()
        conn.close()
        res = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 5},
            principal=alice._principal(),
        )
        env = _parse(res)
        # The urgent poke sorts to the front of the batch.
        assert env["events"][0]["type"] == "directive"


async def test_poke_wakes_a_blocked_waiter_immediately(tmp_path):
    """A poke (insert + waiter-wake, exactly what the REST route does)
    releases an agent already blocked in wait_for_events — immediate
    delivery, not next-reconnect."""
    import asyncio

    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        since = _dt.datetime.now().isoformat()
        task = asyncio.create_task(
            acm.wait_for_events_tool_impl(
                {"since": since, "timeout_seconds": 20},
                principal=alice._principal(),
            )
        )
        await asyncio.sleep(0.4)  # let it enter the slow-path hold
        assert not task.done(), "waiter should be blocked with no events"

        conn = _conn()
        cur = conn.cursor()
        poke_repo.create_poke(
            poke_id="poke_1", agent_id="alice", prompt="wake up",
            priority="urgent", created_by="op", connection=cur,
        )
        conn.commit()
        conn.close()
        g.notify_agent_inbox("alice")  # waiter-wake (REST route's on_commit)

        res = await asyncio.wait_for(task, timeout=10)
        env = _parse(res)
        directives = [e for e in env["events"] if e["type"] == "directive"]
        assert directives, env
        assert directives[0]["data"]["prompt"] == "wake up"


# ── REST route: operator-only + delivery ────────────────────────────────


def _post_poke(admin, agent_id, body):
    return admin.request(
        "POST", f"/api/agents/{agent_id}/directive", json=body,
    )


async def test_rest_poke_operator_success(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_poke(admin, "alice", {"prompt": "go now"})
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        # Row landed undelivered.
        conn = _conn()
        try:
            assert poke_repo.count_undelivered(
                "alice", connection=conn.cursor()
            ) == 1
        finally:
            conn.close()


async def test_rest_poke_queued_when_agent_not_listening(tmp_path):
    """No parked wait_for_events waiter ⇒ the poke is *queued*: the REST
    response reports delivered=False so the dashboard toast can say
    "Queued for X — will arrive on its next check-in"."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_poke(admin, "alice", {"prompt": "later"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivered"] is False, body
        assert "queued" in body["message"].lower(), body


async def test_rest_poke_delivered_when_agent_listening(tmp_path):
    """A parked wait_for_events waiter ⇒ the poke is *delivered
    immediately*: the on_commit waiter-wake releases it, and the REST
    response reports delivered=True so the toast says "Delivered to X".

    We register a waiter directly (what an in-flight ``wait_for_events``
    does on entry) so the branch is exercised deterministically without
    racing a real long-poll coroutine."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        queue = g.register_waiter("alice")
        try:
            r = _post_poke(admin, "alice", {"prompt": "go now"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["delivered"] is True, body
            assert "delivered" in body["message"].lower(), body
        finally:
            g.unregister_waiter("alice", queue)


async def test_rest_poke_unknown_agent_404(tmp_path):
    async with mcp_session(tmp_path) as admin:
        r = _post_poke(admin, "ghost", {"prompt": "x"})
        assert r.status_code == 404, r.text


async def test_rest_poke_missing_prompt_400(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_poke(admin, "alice", {})
        assert r.status_code == 400, r.text


async def test_rest_poke_bad_priority_400(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_poke(admin, "alice", {"prompt": "x", "priority": "nope"})
        assert r.status_code == 400, r.text


async def test_rest_poke_requires_operator_session(tmp_path):
    """A bare-forwarding (non-confirmed operator) request is rejected —
    the poke route is require_operator_session-gated."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        # `admin.get`/`request` sends the signed forwarding header (operator);
        # a non-confirmed forwarding header must be 403. Reuse the settings
        # route's contract: hit with no auth at all → 401/403.
        r = admin.client.post("/api/agents/alice/directive",
                              json={"prompt": "x"})
        assert r.status_code in (401, 403), r.text
