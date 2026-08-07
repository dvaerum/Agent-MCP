"""Operator-driven Disconnect / Reconnect of agents.

"Disconnect" tells a monitoring agent to stop its ``wait_for_events``
loop and drops its live MCP push stream, so an operator can say "we're
done for now" and resume weeks later — WITHOUT terminating the agent or
revoking its token. The mechanism reuses the proven event-coordination
primitives:

  1. ``agents.auto_event_loop`` OFF  → the load-bearing part: every
     ``wait_for_events`` POST now returns ``stop_listening`` (with an
     operator-facing reason) so the loop exits and stays exited even if
     the client reconnects its transport.
  2. ``wake_for_flag_recheck``       → wake the parked long-poll NOW so
     the ``stop_listening`` (the "why + when" message) is delivered
     immediately, not on the next ~5s tick.
  3. ``close_streams_for_agent``     → drop the live GET /mcp SSE push
     stream so the agent flips to OFFLINE in the dashboard right away.

"Disconnect all" is the fleet master switch: it flips the GLOBAL
``config_auto_event_loop_global`` toggle OFF (waking every parked
waiter) and closes every live stream. "Reconnect" / "Reconnect all"
flip the respective flag back ON.

These are operator-tier (``agents.terminate`` capability — the same
gate ``edit_agent`` uses, since Disconnect is literally a scoped edit of
``auto_event_loop`` plus a stream teardown).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_mcp.core.tool_result import NotFound, Ok, PermissionDenied
from tests.harness import make_principal, mcp_session, seed_agent_rows

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────


def _operator():
    return make_principal(
        kind="operator_session",
        user_id="op",
        project_name="demo-project",
        project_role="operator",
    )


def _worker(agent_id: str = "wkr"):
    return make_principal(
        kind="agent_bearer",
        agent_id=agent_id,
        agent_role="worker",
        source_token="dummy",
    )


def _read_auto_event_loop(agent_id: str) -> int:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT auto_event_loop FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"agent {agent_id!r} not found"
    return int(row["auto_event_loop"])


def _attach_live_stream(agent_id: str) -> asyncio.Queue:
    """Register an mcp_sessions row + attach a runtime queue, simulating
    a live GET /mcp SSE stream so ``close_streams_for_agent`` has
    something to signal. Returns the queue so the test can assert the
    ``CLOSE_STREAM`` sentinel landed on it."""
    from agent_mcp.core import session_registry

    sid = session_registry.register_session(
        agent_id=agent_id, bearer_token=f"__test_seed_{agent_id}",
    )
    q: asyncio.Queue = asyncio.Queue()
    session_registry.attach_runtime_queue(sid, q)
    return q


def _queue_got_close(q: asyncio.Queue) -> bool:
    """Drain the runtime queue and report whether the ``CLOSE_STREAM``
    sentinel is among what was enqueued. The operator live-update fanout
    (a ``notifications/resources/updated`` push triggered by the
    ``disconnected_agent`` audit row) legitimately lands on the same
    queue AHEAD of the close sentinel — the pump drains that then hits
    the close — so we scan the whole queue, not just its head."""
    from agent_mcp.core import session_registry

    while not q.empty():
        if q.get_nowait() is session_registry.CLOSE_STREAM:
            return True
    return False


# ── per-agent Disconnect ─────────────────────────────────────────────


async def test_disconnect_sets_auto_event_loop_off(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl

    async with mcp_session(tmp_path):
        seed_agent_rows("alpha")
        assert _read_auto_event_loop("alpha") == 1  # default ON

        result = await disconnect_agent_tool_impl(
            {"agent_id": "alpha"}, principal=_operator(),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert _read_auto_event_loop("alpha") == 0


async def test_disconnect_closes_live_stream(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl

    async with mcp_session(tmp_path):
        seed_agent_rows("beta")
        q = _attach_live_stream("beta")

        result = await disconnect_agent_tool_impl(
            {"agent_id": "beta"}, principal=_operator(),
        )

        assert isinstance(result, Ok)
        assert result.data["closed_streams"] == 1
        # The pump's CLOSE_STREAM sentinel must be on the queue.
        assert _queue_got_close(q)


async def test_disconnect_wakes_parked_waiter(tmp_path, monkeypatch) -> None:
    from agent_mcp.core import globals as g
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl

    woke: list[str] = []
    monkeypatch.setattr(
        g, "wake_for_flag_recheck", lambda aid: woke.append(aid),
    )

    async with mcp_session(tmp_path):
        seed_agent_rows("gamma")
        await disconnect_agent_tool_impl(
            {"agent_id": "gamma"}, principal=_operator(),
        )

    assert woke == ["gamma"], (
        "disconnect must wake the parked wait_for_events so stop_listening "
        "is delivered immediately"
    )


async def test_disconnect_reason_is_operator_facing(tmp_path) -> None:
    """After disconnect, the flag-check reason the agent receives in its
    ``stop_listening`` must explain WHY (operator paused it) and WHEN
    (may resume later) — not the bare 'auto_event_loop is OFF'."""
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl
    from agent_mcp.tools.agent_communication_tools import (
        _check_auto_event_loop_flags,
    )

    async with mcp_session(tmp_path):
        seed_agent_rows("delta")
        await disconnect_agent_tool_impl(
            {"agent_id": "delta"}, principal=_operator(),
        )

        enabled, reason = _check_auto_event_loop_flags("delta")
        assert enabled is False
        low = (reason or "").lower()
        assert "paused" in low or "disconnect" in low, reason
        assert "resume" in low or "later" in low, reason


async def test_disconnect_requires_operator(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl

    async with mcp_session(tmp_path):
        seed_agent_rows("eps")
        result = await disconnect_agent_tool_impl(
            {"agent_id": "eps"}, principal=_worker("eps"),
        )
        assert isinstance(result, PermissionDenied)
        assert _read_auto_event_loop("eps") == 1  # unchanged


async def test_disconnect_unknown_agent_not_found(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import disconnect_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await disconnect_agent_tool_impl(
            {"agent_id": "ghost"}, principal=_operator(),
        )
        assert isinstance(result, NotFound)


# ── per-agent Reconnect ──────────────────────────────────────────────


async def test_reconnect_sets_auto_event_loop_on(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import (
        disconnect_agent_tool_impl,
        reconnect_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        seed_agent_rows("zeta")
        await disconnect_agent_tool_impl(
            {"agent_id": "zeta"}, principal=_operator(),
        )
        assert _read_auto_event_loop("zeta") == 0

        result = await reconnect_agent_tool_impl(
            {"agent_id": "zeta"}, principal=_operator(),
        )
        assert isinstance(result, Ok)
        assert _read_auto_event_loop("zeta") == 1


# ── fleet Disconnect-all / Reconnect-all ─────────────────────────────


async def test_disconnect_all_flips_global_off_and_closes_streams(
    tmp_path,
) -> None:
    from agent_mcp.tools import access
    from agent_mcp.tools.admin_tools import disconnect_all_agents_tool_impl

    async with mcp_session(tmp_path):
        seed_agent_rows("m1", "m2")
        q1 = _attach_live_stream("m1")
        q2 = _attach_live_stream("m2")
        assert access._get_config_bool("config_auto_event_loop_global") is True

        result = await disconnect_all_agents_tool_impl(
            {}, principal=_operator(),
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert (
            access._get_config_bool("config_auto_event_loop_global") is False
        )
        assert _queue_got_close(q1)
        assert _queue_got_close(q2)
        assert result.data["closed_streams"] >= 2


async def test_disconnect_all_wakes_all_waiters(tmp_path, monkeypatch) -> None:
    from agent_mcp.core import globals as g
    from agent_mcp.tools.admin_tools import disconnect_all_agents_tool_impl

    calls: list[int] = []
    monkeypatch.setattr(
        g, "wake_all_for_flag_recheck", lambda: calls.append(1),
    )

    async with mcp_session(tmp_path):
        await disconnect_all_agents_tool_impl({}, principal=_operator())

    assert calls, "disconnect-all must wake every parked waiter"


async def test_reconnect_all_flips_global_on(tmp_path) -> None:
    from agent_mcp.tools import access
    from agent_mcp.tools.admin_tools import (
        disconnect_all_agents_tool_impl,
        reconnect_all_agents_tool_impl,
    )

    async with mcp_session(tmp_path):
        await disconnect_all_agents_tool_impl({}, principal=_operator())
        assert (
            access._get_config_bool("config_auto_event_loop_global") is False
        )

        result = await reconnect_all_agents_tool_impl(
            {}, principal=_operator(),
        )
        assert isinstance(result, Ok)
        assert (
            access._get_config_bool("config_auto_event_loop_global") is True
        )


async def test_disconnect_all_requires_operator(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import disconnect_all_agents_tool_impl

    async with mcp_session(tmp_path):
        result = await disconnect_all_agents_tool_impl(
            {}, principal=_worker(),
        )
        assert isinstance(result, PermissionDenied)
