"""Idle backlog reminder (core/idle_reminder.py) + its wait_for_events wiring."""

from __future__ import annotations

import datetime
import json

import pytest

from agent_mcp.core import idle_reminder as ir


@pytest.fixture(autouse=True)
def _clean():
    ir.clear()
    yield
    ir.clear()


# ── timer ───────────────────────────────────────────────────────────────


def test_first_sight_waits_a_full_interval():
    # Seeds on first sight and returns the whole interval (not 0), so a fresh
    # connection isn't reminded immediately.
    assert ir.seconds_until_due("a", 3600.0, 1000.0) == 3600.0
    # Now seeded at 1000; 100s later, ~3500 remain.
    assert ir.seconds_until_due("a", 3600.0, 1100.0) == pytest.approx(3500.0)


def test_mark_checked_advances_timer():
    ir.seconds_until_due("a", 100.0, 1000.0)  # seed
    ir.mark_checked("a", 2000.0)
    assert ir.seconds_until_due("a", 100.0, 2050.0) == pytest.approx(50.0)
    assert ir.seconds_until_due("a", 100.0, 2200.0) == 0.0  # overdue → 0


# ── backlog collection ──────────────────────────────────────────────────


def _patch_repos(monkeypatch, *, unread_rows, unread_count, tasks):
    from agent_mcp.repositories import message_repo

    monkeypatch.setattr(message_repo, "query", lambda f, **k: unread_rows)
    monkeypatch.setattr(message_repo, "count_unread", lambda a: unread_count)
    monkeypatch.setattr(
        "agent_mcp.repositories.task_repository.get_tasks_by_agent_id",
        lambda a, **k: tasks,
    )


def test_no_backlog_returns_none(monkeypatch):
    _patch_repos(monkeypatch, unread_rows=[], unread_count=0, tasks=[])
    assert ir.collect_backlog("a") is None


def test_backlog_excludes_terminal_tasks(monkeypatch):
    _patch_repos(
        monkeypatch,
        unread_rows=[{"message_id": "m1", "sender_id": "boss", "subject": "do it"}],
        unread_count=1,
        tasks=[
            {"task_id": "t1", "title": "open one", "status": "in_progress"},
            {"task_id": "t2", "title": "done", "status": "completed"},
            {"task_id": "t3", "title": "gone", "status": "cancelled"},
            {"task_id": "t4", "title": "broke", "status": "failed"},
            {"task_id": "t5", "title": "new", "status": "pending"},
        ],
    )
    bl = ir.collect_backlog("a")
    assert bl["unread_count"] == 1
    # Only in_progress + pending survive (completed/cancelled/failed dropped).
    assert bl["task_count"] == 2
    assert {t["task_id"] for t in bl["open_tasks"]} == {"t1", "t5"}


def test_backlog_uses_content_preview_when_no_subject(monkeypatch):
    _patch_repos(
        monkeypatch,
        unread_rows=[
            {"message_id": "m", "sender_id": "x", "message_content": "hello there"}
        ],
        unread_count=1,
        tasks=[],
    )
    bl = ir.collect_backlog("a")
    assert bl["unread_messages"][0]["subject"] == "hello there"


# ── formatting / event shape ─────────────────────────────────────────────


def test_reminder_event_has_count_and_itemized_list():
    backlog = {
        "unread_count": 4,
        "task_count": 2,
        "unread_messages": [
            {"message_id": "m1", "sender_id": "manager", "subject": "status?"},
        ],
        "open_tasks": [
            {"task_id": "t1", "title": "Ship the fix", "status": "in_progress"},
            {"task_id": "t2", "title": "Review PR", "status": "pending"},
        ],
    }
    ev = ir.reminder_event(backlog)
    assert ev["type"] == "reminder"
    assert ev["payload"]["unread_count"] == 4
    assert ev["payload"]["task_count"] == 2
    msg = ev["payload"]["message"]
    # The literal counts appear...
    assert "Unread messages (4)" in msg
    assert "Open tasks (2)" in msg
    # ...and the items are itemized, not just summarized...
    assert "from manager: status?" in msg
    assert "[in_progress] Ship the fix (t1)" in msg
    # ...with a "… and N more" when the count exceeds the shown rows.
    assert "and 3 more" in msg


# ── integration through wait_for_events ─────────────────────────────────


@pytest.mark.asyncio
async def test_reminder_fires_in_loop_when_backlog(tmp_path, monkeypatch):
    """An idle agent with a backlog gets a reminder event with the list."""
    from agent_mcp.tools import agent_communication_tools as act
    from tests.harness import mcp_session, with_bearer

    fake = {
        "unread_count": 1,
        "task_count": 1,
        "unread_messages": [{"message_id": "m", "sender_id": "boss", "subject": "do it"}],
        "open_tasks": [{"task_id": "t", "title": "Ship", "status": "in_progress"}],
    }
    monkeypatch.setattr(ir, "collect_backlog", lambda a: fake)

    async with mcp_session(tmp_path) as admin:
        w = await admin.create_worker("rem-worker")
        ir.clear()
        # Seed the timer as already overdue → the reminder is due immediately.
        ir._last_check[w.agent_id] = -1e9
        # since = now → the fast path finds no new events → enters the hold.
        since = datetime.datetime.now().isoformat()
        with with_bearer(w.token):
            res = await act.wait_for_events_tool_impl(
                {"timeout_seconds": 3, "since": since},
                principal=w._principal(),
            )
        payload = json.loads(res.message)
        types = [e.get("type") for e in payload["events"]]
        assert "reminder" in types, f"no reminder in {types}"
        rem = next(e for e in payload["events"] if e.get("type") == "reminder")
        assert "Ship" in rem["payload"]["message"]


@pytest.mark.asyncio
async def test_no_reminder_when_no_backlog(tmp_path, monkeypatch):
    """Idle + empty backlog → no reminder; the poll just returns empty."""
    import asyncio

    from agent_mcp.tools import agent_communication_tools as act
    from tests.harness import mcp_session, with_bearer

    monkeypatch.setattr(ir, "collect_backlog", lambda a: None)  # no backlog

    async with mcp_session(tmp_path) as admin:
        w = await admin.create_worker("rem-empty")
        ir.clear()
        ir._last_check[w.agent_id] = -1e9  # would be due, but no backlog
        since = datetime.datetime.now().isoformat()
        with with_bearer(w.token):
            res = await asyncio.wait_for(
                act.wait_for_events_tool_impl(
                    {"timeout_seconds": 2, "since": since},
                    principal=w._principal(),
                ),
                timeout=6,
            )
        payload = json.loads(res.message)
        types = [e.get("type") for e in payload["events"]]
        assert "reminder" not in types
