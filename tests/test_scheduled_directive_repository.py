"""Unit tests for scheduled_directive_repository — the firing math.

Covers the locked decisions at the store layer (plan §2):
* interval-reset-from-delivery (3),
* first-fire / run_count bookkeeping,
* end-conditions until/count → terminal (10),
* offline-across-due fires ONCE on reconnect (12).

Uses the ``mcp_session`` harness only to bootstrap a DB, then drives the
repository directly on a raw connection cursor.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.harness import mcp_session
from agent_mcp.repositories import scheduled_directive_repository as repo


pytestmark = pytest.mark.asyncio


def _conn():
    from agent_mcp.db.connection import get_db_connection

    return get_db_connection()


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat()


async def test_fire_resets_next_due_from_delivery(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        # next_due is in the PAST (overdue by 5 min); interval 60s.
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="check the build",
            interval_seconds=60,
            next_due_at=_iso(now - _dt.timedelta(minutes=5)),
            until_at=None,
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        conn.commit()

        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()

        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "directive"
        assert ev["ref_id"] == "d1"
        assert ev["priority"] == "urgent"
        assert ev["data"]["prompt"] == "check the build"
        assert ev["data"]["source"] == "schedule"
        assert ev["data"]["schedule_id"] == "d1"

        row = repo.get("d1", connection=cur)
        assert row["run_count"] == 1
        # Reset from DELIVERY (now), not the old grid — now + 60s.
        assert row["next_due_at"] == _iso(now + _dt.timedelta(seconds=60))
        assert row["status"] == "active"
        assert row["enabled"] == 1
        conn.close()


async def test_offline_across_many_intervals_fires_once(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 4, 12, 0, 0)  # 3 days later
        # next_due 3 days ago, 15-min interval → 288 missed slots.
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="poll",
            interval_seconds=900,
            next_due_at=_iso(now - _dt.timedelta(days=3)),
            until_at=None,
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        # Fires ONCE, not 288×.
        assert len(events) == 1
        row = repo.get("d1", connection=cur)
        assert row["run_count"] == 1
        assert row["next_due_at"] == _iso(now + _dt.timedelta(seconds=900))
        conn.close()


async def test_max_runs_end_condition_completes(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="x",
            interval_seconds=60,
            next_due_at=_iso(now - _dt.timedelta(seconds=1)),
            until_at=None,
            max_runs=1,
            created_by="alice",
            connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        assert len(events) == 1  # the single allowed fire is delivered
        row = repo.get("d1", connection=cur)
        assert row["run_count"] == 1
        assert row["status"] == "completed"
        assert row["enabled"] == 0
        # A completed schedule is no longer fireable.
        assert repo.has_active("alice", _iso(now), connection=cur) is False
        conn.close()


async def test_until_window_next_fire_beyond_completes(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        # until is 30s out; interval 60s → next fire (now+60) is beyond
        # the window, so this fire is the last.
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="x",
            interval_seconds=60,
            next_due_at=_iso(now),
            until_at=_iso(now + _dt.timedelta(seconds=30)),
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        assert len(events) == 1
        row = repo.get("d1", connection=cur)
        assert row["status"] == "completed"
        assert row["enabled"] == 0
        conn.close()


async def test_until_already_passed_reaps_without_firing(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        # until_at already in the past; next_due also past. Past-window →
        # reaped, no event emitted.
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="x",
            interval_seconds=60,
            next_due_at=_iso(now - _dt.timedelta(minutes=5)),
            until_at=_iso(now - _dt.timedelta(minutes=1)),
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        assert events == []
        row = repo.get("d1", connection=cur)
        assert row["status"] == "completed"
        assert row["enabled"] == 0
        assert row["run_count"] == 0
        conn.close()


async def test_not_yet_due_does_not_fire(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        future = _iso(now + _dt.timedelta(seconds=60))
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="x",
            interval_seconds=60,
            next_due_at=future,
            until_at=None,
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        assert events == []
        # soonest_due reflects the pending fire; agent has an active sched.
        assert repo.soonest_due_at("alice", _iso(now), connection=cur) == future
        assert repo.has_active("alice", _iso(now), connection=cur) is True
        conn.close()


async def test_disabled_schedule_never_fires_or_counts(tmp_path) -> None:
    async with mcp_session(tmp_path):
        conn = _conn()
        cur = conn.cursor()
        now = _dt.datetime(2026, 1, 1, 12, 0, 0)
        repo.create(
            directive_id="d1",
            agent_id="alice",
            prompt="x",
            interval_seconds=60,
            next_due_at=_iso(now - _dt.timedelta(minutes=5)),
            until_at=None,
            max_runs=None,
            created_by="alice",
            connection=cur,
        )
        repo.update_fields(
            "d1", {"enabled": 0, "status": "paused"},
            updated_by="alice", connection=cur,
        )
        conn.commit()
        events = repo.collect_due_and_fire("alice", _iso(now), connection=cur)
        conn.commit()
        assert events == []
        assert repo.count_active_for_agent("alice", connection=cur) == 0
        assert repo.has_active("alice", _iso(now), connection=cur) is False
        conn.close()
