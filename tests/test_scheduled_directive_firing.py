"""Integration: scheduled directives fire through the wait_for_events
slice loop (plan §4, §10 wire behaviours).

Covers: due schedule fires on check-in; not-yet-due holds; idle hold wakes
at next_due; offline-across-due fires ONCE on reconnect; an enabled
schedule suppresses idle-stop.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

import agent_mcp.tools.agent_communication_tools as acm
from agent_mcp.repositories import scheduled_directive_repository as repo
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _parse(result) -> dict:
    from agent_mcp.core.tool_result import render_as_text_content

    return json.loads(render_as_text_content(result)[0].text)


def _types(env: dict) -> list:
    return [e["type"] for e in env["events"]]


def _seed_schedule(agent_id, *, next_due_at, interval_seconds=60,
                   until_at=None, max_runs=None, directive_id="d1",
                   prompt="do the thing"):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        repo.create(
            directive_id=directive_id,
            agent_id=agent_id,
            prompt=prompt,
            interval_seconds=interval_seconds,
            next_due_at=next_due_at,
            until_at=until_at,
            max_runs=max_runs,
            created_by=agent_id,
            connection=cur,
        )
        conn.commit()
    finally:
        conn.close()


def _get(directive_id="d1"):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        return repo.get(directive_id, connection=conn.cursor())
    finally:
        conn.close()


def _set_config(key, value):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = _dt.datetime.now().isoformat()
        cur.execute(
            "INSERT INTO project_settings (context_key, value, created_at, "
            "created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(context_key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value), now, "t", now, "t"),
        )
        conn.commit()
    finally:
        conn.close()


def _set_last_activity(agent_id, iso):
    from agent_mcp.repositories import agent_repo

    agent_repo.update_field(agent_id, "last_activity_at", iso)


async def test_due_schedule_fires_on_check_in(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            "alice",
            next_due_at=(now - _dt.timedelta(seconds=5)).isoformat(),
        )
        since = now.isoformat()
        res = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 5},
            principal=alice._principal(),
        )
        env = _parse(res)
        assert "directive" in _types(env), env
        directive = next(e for e in env["events"] if e["type"] == "directive")
        assert directive["data"]["source"] == "schedule"
        assert directive["data"]["prompt"] == "do the thing"
        # next_due reset from delivery (future now).
        row = _get()
        assert row["run_count"] == 1
        assert row["next_due_at"] > since


async def test_offline_across_due_fires_once(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        # 3 days overdue, 15-min interval.
        _seed_schedule(
            "alice",
            interval_seconds=900,
            next_due_at=(now - _dt.timedelta(days=3)).isoformat(),
        )
        since = (now - _dt.timedelta(days=3, seconds=1)).isoformat()
        res = await acm.wait_for_events_tool_impl(
            {"since": since, "timeout_seconds": 5},
            principal=alice._principal(),
        )
        env = _parse(res)
        directives = [e for e in env["events"] if e["type"] == "directive"]
        assert len(directives) == 1, env
        # Second check-in from the advanced cursor: nothing due now.
        res2 = await acm.wait_for_events_tool_impl(
            {"since": env["next_cursor"], "timeout_seconds": 1},
            principal=alice._principal(),
        )
        env2 = _parse(res2)
        assert [e for e in env2["events"] if e["type"] == "directive"] == []


async def test_not_yet_due_does_not_fire(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            "alice",
            next_due_at=(now + _dt.timedelta(hours=1)).isoformat(),
        )
        res = await acm.wait_for_events_tool_impl(
            {"since": now.isoformat(), "timeout_seconds": 1},
            principal=alice._principal(),
        )
        env = _parse(res)
        assert [e for e in env["events"] if e["type"] == "directive"] == []
        assert _get()["run_count"] == 0


async def test_idle_hold_wakes_at_next_due(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        # Due ~1.5s from now: the idle hold must wake and fire it.
        _seed_schedule(
            "alice",
            next_due_at=(now + _dt.timedelta(seconds=1.5)).isoformat(),
        )
        start = _dt.datetime.now()
        res = await acm.wait_for_events_tool_impl(
            {"since": now.isoformat(), "timeout_seconds": 20},
            principal=alice._principal(),
        )
        elapsed = (_dt.datetime.now() - start).total_seconds()
        env = _parse(res)
        assert "directive" in _types(env), env
        # Woke roughly at next_due, well before the 20s hold cap.
        assert elapsed < 10, elapsed


async def test_enabled_schedule_suppresses_idle_stop(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 1)
        _set_last_activity(
            "alice", (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
        )
        now = _dt.datetime.now()
        # An enabled schedule due far in the future — idle-stop must NOT
        # fire while it exists.
        _seed_schedule(
            "alice",
            next_due_at=(now + _dt.timedelta(hours=1)).isoformat(),
        )
        res = await acm.wait_for_events_tool_impl(
            {"since": now.isoformat(), "timeout_seconds": 2},
            principal=alice._principal(),
        )
        env = _parse(res)
        assert "stop_listening" not in _types(env), env


async def test_no_schedule_still_idle_stops(tmp_path):
    """Control: same idle setup WITHOUT a schedule → stop_listening."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_event_idle_stop_seconds", 1)
        _set_last_activity(
            "alice", (_dt.datetime.now() - _dt.timedelta(hours=2)).isoformat()
        )
        res = await acm.wait_for_events_tool_impl(
            {"since": _dt.datetime.now().isoformat(), "timeout_seconds": 5},
            principal=alice._principal(),
        )
        env = _parse(res)
        assert _types(env) == ["stop_listening"], env
