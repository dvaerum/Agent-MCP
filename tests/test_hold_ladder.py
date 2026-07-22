"""Adaptive hold ladder for wait_for_events (core/hold_ladder.py)."""

from __future__ import annotations

import pytest

from agent_mcp.core import hold_ladder as hl


@pytest.fixture(autouse=True)
def _clean():
    hl.clear()
    yield
    hl.clear()


# ── counter ────────────────────────────────────────────────────────────


def test_counter_increments_and_resets():
    assert hl.get_count("a") == 0
    assert hl.note_empty_short_poll("a") == 1
    assert hl.note_empty_short_poll("a") == 2
    assert hl.get_count("a") == 2
    hl.reset("a")
    assert hl.get_count("a") == 0


def test_counters_are_per_agent():
    hl.note_empty_short_poll("a")
    hl.note_empty_short_poll("a")
    hl.note_empty_short_poll("b")
    assert hl.get_count("a") == 2
    assert hl.get_count("b") == 1
    hl.reset("a")
    assert hl.get_count("a") == 0
    assert hl.get_count("b") == 1


# ── decision ladder ────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [0, 1, 5, hl.ADVISE_AFTER - 1])
def test_below_threshold_is_normal(count):
    d = hl.decide(count)
    assert d.phase == "normal"
    assert d.override_hold is False
    assert d.advisory is None


@pytest.mark.parametrize("count", range(hl.ADVISE_AFTER, hl.OVERRIDE_AFTER))
def test_advise_band_advises_without_overriding(count):
    d = hl.decide(count)
    assert d.phase == "advise"
    assert d.override_hold is False
    assert d.advisory and "timeout_seconds" in d.advisory


@pytest.mark.parametrize("count", [hl.OVERRIDE_AFTER, hl.OVERRIDE_AFTER + 50])
def test_override_band_parks(count):
    d = hl.decide(count)
    assert d.phase == "override"
    assert d.override_hold is True


def test_escalation_gets_stronger():
    first = hl.decide(hl.ADVISE_AFTER).advisory
    last = hl.decide(hl.OVERRIDE_AFTER - 1).advisory
    # The last advise step before override is a FINAL NOTICE; the first is not.
    assert "FINAL NOTICE" in last
    assert "FINAL NOTICE" not in first


def test_advisory_event_shape():
    ev = hl.advisory_event("stop it")
    assert ev["type"] == "hold_advisory"
    assert ev["payload"]["message"] == "stop it"
    assert "timestamp" in ev


# ── integration through wait_for_events ────────────────────────────────


async def _call_wait(worker, *, timeout_seconds, progress_token, client="claude-code"):
    """Drive wait_for_events_tool_impl directly with a controlled
    progressToken + recorded client identity, so we exercise the ladder's
    eligibility gate (heartbeat client + progressToken + short cap).

    ``current_progress_token`` reads the SDK request context, which the
    harness has no live session for — patch it to the desired value. The
    tool re-imports it from the module on each call, so patching the module
    attr takes effect.
    """
    from unittest.mock import patch

    from agent_mcp.tools import agent_communication_tools as act
    from agent_mcp.core import client_info_registry, mcp_progress
    from tests.harness import with_bearer

    client_info_registry.record_client_info(worker.agent_id, client, "1.0")
    with patch.object(
        mcp_progress, "current_progress_token", lambda: progress_token
    ):
        with with_bearer(worker.token):
            return await act.wait_for_events_tool_impl(
                {"timeout_seconds": timeout_seconds},
                principal=worker._principal(),
            )


@pytest.mark.asyncio
async def test_advise_attaches_hold_advisory(tmp_path):
    """At the ADVISE threshold, an eligible empty short-poll comes back with a
    hold_advisory event nudging the agent to drop the timeout."""
    import json
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        w = await admin.create_worker("ladder-advise")
        hl.clear()
        hl._counts[w.agent_id] = hl.ADVISE_AFTER  # pre-seed into the advise band
        result = await _call_wait(
            w, timeout_seconds=1, progress_token="ptok",
        )
        payload = json.loads(result.message)
        types = [e.get("type") for e in payload["events"]]
        assert "hold_advisory" in types, f"no advisory in {types}"
        # ...and the run advanced past the seed.
        assert hl.get_count(w.agent_id) == hl.ADVISE_AFTER + 1


@pytest.mark.asyncio
async def test_override_ignores_short_timeout_and_parks(tmp_path):
    """At the OVERRIDE threshold, a 1s timeout is ignored — the server parks
    the connection (does NOT return at ~1s)."""
    import asyncio
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        w = await admin.create_worker("ladder-override")
        hl.clear()
        hl._counts[w.agent_id] = hl.OVERRIDE_AFTER  # into the override band
        with pytest.raises(asyncio.TimeoutError):
            # If the cap were honoured it'd return at ~1s; overridden it parks.
            await asyncio.wait_for(
                _call_wait(w, timeout_seconds=1, progress_token="ptok"),
                timeout=4,
            )


@pytest.mark.asyncio
async def test_no_progress_token_is_not_eligible_no_park(tmp_path):
    """A heartbeat client WITHOUT a progressToken must NOT be parked (its own
    idle watchdog would kill a silent hold) — even seeded past override, the
    short 1s timeout is honoured."""
    import asyncio
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        w = await admin.create_worker("ladder-noptok")
        hl.clear()
        hl._counts[w.agent_id] = hl.OVERRIDE_AFTER
        # progress_token=None → not eligible → cap honoured → returns ~1s.
        result = await asyncio.wait_for(
            _call_wait(w, timeout_seconds=1, progress_token=None),
            timeout=5,
        )
        assert result is not None  # returned (did not park)
        # not eligible → ladder was reset.
        assert hl.get_count(w.agent_id) == 0
