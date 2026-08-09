"""CRUD + three-tier authz + guardrail tests for the scheduled-directive
tools (plan §5, §2 decisions 5/6/8/10).
"""

from __future__ import annotations

import datetime as _dt

import pytest

import agent_mcp.tools.scheduled_directive_tools as sdt
from agent_mcp.core.tool_result import Invalid, NotFound, Ok, PermissionDenied
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _set_config(key: str, value) -> None:
    import json

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = _dt.datetime.now().isoformat()
        cur.execute(
            "INSERT INTO project_settings (context_key, value, created_at, "
            "created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(context_key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value), now, "test", now, "test"),
        )
        conn.commit()
    finally:
        conn.close()


async def _promote_to_manager(admin, session) -> None:
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.cursor().execute(
            "UPDATE agents SET agent_role='manager' WHERE agent_id=?",
            (session.agent_id,),
        )
        conn.commit()
    finally:
        conn.close()
    if session.token in g.active_agents:
        g.active_agents[session.token]["agent_role"] = "manager"


# ── create + first-fire semantics ──────────────────────────────────────


async def test_worker_creates_own_schedule_first_fire_one_interval(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        before = _dt.datetime.now()
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "check build", "interval_seconds": 120},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        d = res.data["directive"]
        assert d["agent_id"] == "alice"
        assert d["interval_seconds"] == 120
        assert d["enabled"] is True
        assert d["run_count"] == 0
        # First fire is ~one interval out (not immediate).
        nd = _dt.datetime.fromisoformat(d["next_due_at"])
        assert nd >= before + _dt.timedelta(seconds=119)


async def test_run_now_first_fire_immediate(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        before = _dt.datetime.now()
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "go", "interval_seconds": 60, "run_now": True},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        nd = _dt.datetime.fromisoformat(res.data["directive"]["next_due_at"])
        # Immediate: next_due is at/around now, not a minute out.
        assert nd <= before + _dt.timedelta(seconds=2)


async def test_create_with_count_end_condition(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "count": 3},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        assert res.data["directive"]["max_runs"] == 3


# ── guardrails ─────────────────────────────────────────────────────────


async def test_interval_below_floor_rejected(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 30},  # floor is 60
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "interval_seconds"


async def test_custom_floor_enforced(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_min_schedule_interval_seconds", 300)
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 120},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res


async def test_max_schedules_per_agent_enforced(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_max_schedules_per_agent", 2)
        p = alice._principal()
        for i in range(2):
            r = await sdt.create_scheduled_directive_tool_impl(
                {"prompt": f"x{i}", "interval_seconds": 60}, principal=p,
            )
            assert isinstance(r, Ok), r
        # Third exceeds the cap.
        r3 = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x3", "interval_seconds": 60}, principal=p,
        )
        assert isinstance(r3, Invalid), r3


# ── three-tier authz ───────────────────────────────────────────────────


async def test_self_schedule_toggle_off_denies_worker(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        _set_config("config_allow_worker_self_schedule", False)
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60},
            principal=alice._principal(),
        )
        assert isinstance(res, PermissionDenied), res


async def test_worker_cannot_schedule_for_another_agent(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await admin.create_worker("bob")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "agent_id": "bob"},
            principal=alice._principal(),
        )
        assert isinstance(res, PermissionDenied), res


async def test_manager_schedules_for_worker(tmp_path):
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr")
        await _promote_to_manager(admin, mgr)
        await admin.create_worker("bob")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "agent_id": "bob"},
            principal=mgr._principal(),
        )
        assert isinstance(res, Ok), res
        assert res.data["directive"]["agent_id"] == "bob"


async def test_manager_cannot_schedule_for_another_manager(tmp_path):
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr")
        await _promote_to_manager(admin, mgr)
        mgr2 = await admin.create_worker("mgr2")
        await _promote_to_manager(admin, mgr2)
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "agent_id": "mgr2"},
            principal=mgr._principal(),
        )
        assert isinstance(res, PermissionDenied), res


async def test_manager_curate_toggle_off_denies(tmp_path):
    async with mcp_session(tmp_path) as admin:
        mgr = await admin.create_worker("mgr")
        await _promote_to_manager(admin, mgr)
        await admin.create_worker("bob")
        _set_config("config_allow_manager_curate_schedules", False)
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "agent_id": "bob"},
            principal=mgr._principal(),
        )
        assert isinstance(res, PermissionDenied), res


async def test_operator_schedules_for_any_agent(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("bob")
        # admin session is operator/sysadmin tier.
        agent = await admin.create_admin_agent("admin")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "agent_id": "bob"},
            principal=agent._principal(),
        )
        assert isinstance(res, Ok), res


# ── list / update / delete ─────────────────────────────────────────────


async def test_list_returns_own_schedules(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = alice._principal()
        await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=p,
        )
        await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "b", "interval_seconds": 90}, principal=p,
        )
        res = await sdt.list_scheduled_directives_tool_impl({}, principal=p)
        assert isinstance(res, Ok), res
        assert res.data["count"] == 2


async def test_update_pause_and_resume(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = alice._principal()
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=p,
        )
        did = c.data["directive"]["directive_id"]
        # Pause.
        r = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": False}, principal=p,
        )
        assert isinstance(r, Ok), r
        assert r.data["directive"]["enabled"] is False
        assert r.data["directive"]["status"] == "paused"
        # Resume.
        r2 = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": True}, principal=p,
        )
        assert isinstance(r2, Ok), r2
        assert r2.data["directive"]["enabled"] is True
        assert r2.data["directive"]["status"] == "active"


async def test_update_interval_revalidates_floor(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = alice._principal()
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=p,
        )
        did = c.data["directive"]["directive_id"]
        r = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "interval_seconds": 5}, principal=p,
        )
        assert isinstance(r, Invalid), r


async def test_delete_removes_schedule(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = alice._principal()
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=p,
        )
        did = c.data["directive"]["directive_id"]
        r = await sdt.delete_scheduled_directive_tool_impl(
            {"directive_id": did}, principal=p,
        )
        assert isinstance(r, Ok), r
        listing = await sdt.list_scheduled_directives_tool_impl({}, principal=p)
        assert listing.data["count"] == 0


async def test_delete_missing_is_not_found(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        r = await sdt.delete_scheduled_directive_tool_impl(
            {"directive_id": "sd_nope"}, principal=alice._principal(),
        )
        assert isinstance(r, NotFound), r


async def test_worker_cannot_update_another_agents_schedule(tmp_path):
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=bob._principal(),
        )
        did = c.data["directive"]["directive_id"]
        alice = await admin.create_worker("alice")
        r = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": False},
            principal=alice._principal(),
        )
        # R17-F2: a non-owner worker must not be able to tell "exists but
        # forbidden" apart from "missing" — collapsed to opaque NotFound.
        assert isinstance(r, NotFound), r
        assert not isinstance(r, PermissionDenied), r
