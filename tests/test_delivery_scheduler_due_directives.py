"""Delivery scheduler — `directive.due` trigger (ADR-0026).

`collect_due_and_fire` (the wait_for_events-native firing step) is only
ever driven by a live `wait_for_events`/`fetch_events_since` call. A
delivery-connected worker that never polls therefore never has its
schedules evaluated — the gap this trigger closes. `tick()` gains a second,
additive call path: for every worker with a live delivery stream, fire due
directives and push each as a `directive_due` delivery frame.

Fixture idiom mirrors `tests/test_delivery_scheduler.py` (autouse
`dt.clear()`/`sched.clear()`); schedule seeding mirrors
`tests/test_scheduled_directive_firing.py`'s `_seed_schedule` helper.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agent_mcp.features import delivery_policy as dp
from agent_mcp.features import delivery_scheduler as sched
from agent_mcp.features import delivery_transport as dt
from agent_mcp.repositories import scheduled_directive_repository as repo
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean():
    dt.clear()
    sched.clear()
    yield
    dt.clear()
    sched.clear()


def _cfg(**over) -> dp.DeliveryPolicyConfig:
    base = {
        "enabled": True,
        "on_unread_messages": True,
        "on_unfinished_tasks": True,
        "on_unassigned_tasks": False,
        "on_due_directives": True,
        "backoff_initial_seconds": 30,
        "backoff_max_seconds": 3600,
        "cooldown_seconds": 60,
        "wake_dormant": False,
    }
    base.update(over)
    return dp.DeliveryPolicyConfig(**base)


def _seed_schedule(
    agent_id,
    *,
    next_due_at,
    interval_seconds=60,
    directive_id="d1",
    prompt="check in with all workers",
):
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
            until_at=None,
            max_runs=None,
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
    """Write one `project_settings` row directly — mirrors
    `test_scheduled_directive_firing.py`'s helper of the same name."""
    import json as _json

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = _dt.datetime.now().isoformat()
        cur.execute(
            "INSERT INTO project_settings (context_key, value, created_at, "
            "created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(context_key) DO UPDATE SET value=excluded.value",
            (key, _json.dumps(value), now, "t", now, "t"),
        )
        conn.commit()
    finally:
        conn.close()


async def test_connected_but_non_polling_agent_gets_nothing_today(
    tmp_path: Path,
):
    """RED before the fix: a connected-but-never-polling worker's overdue
    directive is never evaluated by `tick()` — there is no code path that
    reads scheduled directives outside `wait_for_events`."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now - _dt.timedelta(minutes=20)).isoformat(),
        )
        sub = dt.subscribe(alice.agent_id)  # connected, never polls
        _set_config("config_delivery_enabled", True)

        pushed = sched.tick(now=100.0)

        assert pushed >= 1, "tick() should have fired the overdue directive"
        frame = sub.queue.get_nowait()
        assert frame["reason"] == "directive_due"


async def test_tick_fires_due_directive_and_pushes_frame(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now - _dt.timedelta(minutes=1)).isoformat(),
        )
        sub = dt.subscribe(alice.agent_id)
        _set_config("config_delivery_enabled", True)

        assert sched.tick(now=100.0) >= 1
        frame = sub.queue.get_nowait()
        assert frame["type"] == "delivery"
        assert frame["reason"] == "directive_due"
        directive = frame["directive"]
        assert directive["data"]["prompt"] == "check in with all workers"
        assert directive["data"]["source"] == "schedule"

        row = _get()
        assert row["run_count"] == 1


async def test_not_due_directive_not_pushed(tmp_path: Path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now + _dt.timedelta(hours=1)).isoformat(),
        )
        sub = dt.subscribe(alice.agent_id)
        _set_config("config_delivery_enabled", True)

        assert sched.tick(now=100.0) == 0
        assert sub.queue.empty()
        assert _get()["run_count"] == 0


async def test_disconnected_agent_directive_never_read(tmp_path: Path):
    """Protects offline-fire-once-on-reconnect: tick() must never touch a
    disconnected worker's row (no live delivery stream => not iterated)."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now - _dt.timedelta(minutes=20)).isoformat(),
        )
        # No dt.subscribe(...) — worker never connects a delivery stream.
        _set_config("config_delivery_enabled", True)

        assert sched.tick(now=100.0) == 0
        assert _get()["run_count"] == 0


async def test_disabled_on_due_directives_toggle_suppresses(tmp_path: Path):
    """Master switch on, but the per-trigger toggle off: tick() must not
    fire the due directive even though the worker is connected and armed."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now - _dt.timedelta(minutes=20)).isoformat(),
        )
        sub = dt.subscribe(alice.agent_id)

        _set_config("config_delivery_enabled", True)
        _set_config("config_delivery_on_due_directives", False)

        assert sched.tick() == 0
        assert sub.queue.empty()
        assert _get()["run_count"] == 0


async def test_master_switch_off_suppresses_directive_trigger_too(
    tmp_path: Path,
):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        now = _dt.datetime.now()
        _seed_schedule(
            alice.agent_id,
            next_due_at=(now - _dt.timedelta(minutes=20)).isoformat(),
        )
        sub = dt.subscribe(alice.agent_id)

        # config_delivery_enabled defaults False — tick() must short-circuit
        # before ever reading connected_agent_ids()/directives.
        assert sched.load_config().enabled is False
        assert sched.tick() == 0
        assert sub.queue.empty()
        assert _get()["run_count"] == 0
