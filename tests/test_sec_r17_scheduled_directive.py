"""R17 — scheduled-directive hardening.

R17-F1 (LOW-MED): a timezone-aware ``until`` (offset or trailing ``Z``)
must not raise ``TypeError: can't compare offset-naive and offset-aware``
in ``_validate_until`` (→ raw HTTP 500 / generic Failed). It must be
NORMALIZED to the module's naive-local convention so validation AND the
downstream LEXICAL string comparisons (``next_due`` in create,
``until_at`` in the repository's collect_due_and_fire / soonest_due) stay
coherent. A genuinely-bad string → clean ``Invalid``; a past ``until`` →
clean ``Invalid``; NEVER a 500.

R17-F2 (LOW): update/delete looked the row up FIRST, then authorized —
so a non-owner worker could distinguish "exists but forbidden"
(PermissionDenied) from "missing" (NotFound), an existence oracle.
A non-owner must get the SAME opaque NotFound as a nonexistent id.
"""

from __future__ import annotations

import datetime as _dt

import pytest

import agent_mcp.tools.scheduled_directive_tools as sdt
from agent_mcp.core.tool_result import Failed, Invalid, NotFound, Ok, PermissionDenied
from agent_mcp.repositories import scheduled_directive_repository as repo
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# A comfortably-far-future instant in three tz-aware spellings + a naive
# control. All refer to a moment well past any plausible test clock.
_FUTURE_Z = "2035-01-01T00:00:00Z"
_FUTURE_OFFSET = "2035-01-01T00:00:00+05:00"
_FUTURE_UTC = "2035-01-01T00:00:00+00:00"
_FUTURE_NAIVE = "2035-01-01T00:00:00"
_PAST_Z = "2020-01-01T00:00:00Z"


def _is_naive_iso(value: str) -> bool:
    """True iff ``value`` is a valid ISO datetime with NO tz offset/Z —
    i.e. normalized to the module's naive-local convention."""
    parsed = _dt.datetime.fromisoformat(value)
    return parsed.tzinfo is None


# ── R17-F1: tz-aware ``until`` normalized, never a 500 ──────────────────


@pytest.mark.parametrize("until", [_FUTURE_Z, _FUTURE_OFFSET, _FUTURE_UTC])
async def test_create_tz_aware_until_normalized_not_500(tmp_path, until):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "until": until},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        stored = res.data["directive"]["until_at"]
        # Normalized to naive-local — no offset/Z leaks into storage.
        assert _is_naive_iso(stored), stored
        # And it compares coherently (lexically) against the naive next_due.
        nd = res.data["directive"]["next_due_at"]
        assert _is_naive_iso(nd), nd
        assert nd < stored


async def test_create_naive_until_control_still_ok(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "until": _FUTURE_NAIVE},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        assert res.data["directive"]["until_at"] == _FUTURE_NAIVE


async def test_create_past_tz_aware_until_clean_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "until": _PAST_Z},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "until"


async def test_create_garbage_until_clean_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60, "until": "not-a-date"},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "until"


async def test_update_tz_aware_until_normalized_not_failed(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        p = alice._principal()
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 60}, principal=p,
        )
        did = c.data["directive"]["directive_id"]
        res = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "until": _FUTURE_OFFSET}, principal=p,
        )
        assert isinstance(res, Ok), res
        assert not isinstance(res, Failed), res
        assert _is_naive_iso(res.data["directive"]["until_at"])


async def test_normalized_until_reap_logic_coherent(tmp_path):
    """A far-future tz-aware ``until`` must be treated as IN-window by the
    repository's naive-ISO reap/window logic (proves the stored value is
    normalized, not a tz-aware string that a lexical compare would misread).
    """
    from agent_mcp.db.connection import get_db_connection

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {
                "prompt": "x",
                "interval_seconds": 60,
                "until": _FUTURE_UTC,
                "run_now": True,
            },
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        conn = get_db_connection()
        try:
            now_iso = _dt.datetime.now().isoformat()
            # In-window ⇒ still fireable.
            assert repo.has_active("alice", now_iso, connection=conn.cursor())
            events = repo.collect_due_and_fire(
                "alice", now_iso, connection=conn.cursor()
            )
            conn.commit()
        finally:
            conn.close()
        # It fired (was not spuriously reaped as past-window).
        assert len(events) == 1, events


# ── R17-F1 via REST (operator surface) — no 500 ─────────────────────────


async def test_rest_create_tz_aware_until_not_500(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.request("POST", "/api/schedules", json={
            "agent_id": "alice", "prompt": "x",
            "interval_seconds": 120, "until": _FUTURE_Z,
        })
        assert r.status_code == 200, r.text
        assert _is_naive_iso(r.json()["directive"]["until_at"])


async def test_rest_create_past_until_is_400_not_500(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.request("POST", "/api/schedules", json={
            "agent_id": "alice", "prompt": "x",
            "interval_seconds": 120, "until": _PAST_Z,
        })
        assert r.status_code == 400, r.text


async def test_rest_update_tz_aware_until_not_500(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        did = admin.request("POST", "/api/schedules", json={
            "agent_id": "alice", "prompt": "x", "interval_seconds": 120,
        }).json()["directive"]["directive_id"]
        r = admin.request("PUT", f"/api/schedules/{did}",
                          json={"until": _FUTURE_OFFSET})
        assert r.status_code == 200, r.text
        assert _is_naive_iso(r.json()["directive"]["until_at"])


# ── R17-F2: existence oracle collapsed to opaque NotFound ───────────────


async def _bobs_directive(admin) -> str:
    bob = await admin.create_worker("bob")
    c = await sdt.create_scheduled_directive_tool_impl(
        {"prompt": "a", "interval_seconds": 60}, principal=bob._principal(),
    )
    return c.data["directive"]["directive_id"]


async def test_nonowner_worker_update_gets_notfound_not_unauthorized(tmp_path):
    async with mcp_session(tmp_path) as admin:
        did = await _bobs_directive(admin)
        alice = await admin.create_worker("alice")
        # Real (someone else's) id and a nonexistent id must be
        # indistinguishable to a non-owner worker.
        real = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": False},
            principal=alice._principal(),
        )
        missing = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": "sd_ffffffffffffffff", "enabled": False},
            principal=alice._principal(),
        )
        assert isinstance(real, NotFound), real
        assert not isinstance(real, PermissionDenied), real
        assert isinstance(missing, NotFound), missing


async def test_nonowner_worker_delete_gets_notfound_not_unauthorized(tmp_path):
    async with mcp_session(tmp_path) as admin:
        did = await _bobs_directive(admin)
        alice = await admin.create_worker("alice")
        real = await sdt.delete_scheduled_directive_tool_impl(
            {"directive_id": did}, principal=alice._principal(),
        )
        missing = await sdt.delete_scheduled_directive_tool_impl(
            {"directive_id": "sd_ffffffffffffffff"},
            principal=alice._principal(),
        )
        assert isinstance(real, NotFound), real
        assert not isinstance(real, PermissionDenied), real
        assert isinstance(missing, NotFound), missing
        # And bob's directive is still there (the phantom NotFound did not
        # actually delete it).
        listing = await sdt.list_scheduled_directives_tool_impl(
            {"agent_id": "bob"},
            principal=(await admin.create_admin_agent("op"))._principal(),
        )
        assert any(d["directive_id"] == did for d in listing.data["directives"])


async def test_owner_worker_still_updates_and_deletes(tmp_path):
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        p = bob._principal()
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=p,
        )
        did = c.data["directive"]["directive_id"]
        u = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": False}, principal=p,
        )
        assert isinstance(u, Ok), u
        d = await sdt.delete_scheduled_directive_tool_impl(
            {"directive_id": did}, principal=p,
        )
        assert isinstance(d, Ok), d


async def test_manager_still_updates_workers_schedule(tmp_path):
    async with mcp_session(tmp_path) as admin:
        bob = await admin.create_worker("bob")
        c = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "a", "interval_seconds": 60}, principal=bob._principal(),
        )
        did = c.data["directive"]["directive_id"]
        mgr = await admin.create_worker("mgr")
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            conn.cursor().execute(
                "UPDATE agents SET agent_role='manager' WHERE agent_id=?",
                (mgr.agent_id,),
            )
            conn.commit()
        finally:
            conn.close()
        if mgr.token in g.active_agents:
            g.active_agents[mgr.token]["agent_role"] = "manager"

        u = await sdt.update_scheduled_directive_tool_impl(
            {"directive_id": did, "enabled": False}, principal=mgr._principal(),
        )
        assert isinstance(u, Ok), u
