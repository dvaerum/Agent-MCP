"""AC-R29-1 [revoke-completeness]: terminate/revoke must close an
agent's already-open GET /mcp SSE push stream.

A GET /mcp stream (the server-push channel delivering
``notifications/resources/updated`` nudges — inbox URI /
tools/list_changed) authenticates its bearer ONCE at open, then pumps
indefinitely. Before this fix the pump never re-validated that the
bearer was still live, and ``terminate_agent`` never signalled an
already-open stream — so a stolen/compromised token that opened a
stream BEFORE the operator terminated the agent kept a LIVE push
channel after revocation. The terminated-token-can't-call-tools control
reached every other channel except this one.

These tests pin the fix:

  * the SSE pump SELF-VALIDATES — it tears the stream down once the
    bearer leaves the auth cache (the same cache-only liveness predicate
    the ``/mcp`` gate and per-request tool dispatch use), even with no
    active nudge;
  * ``terminate_agent`` ACTIVELY signals the open stream so teardown is
    immediate (doesn't wait for the next heartbeat self-validation tick);
  * the happy path is untouched — a live agent's stream keeps pumping
    heartbeats and delivered payloads across re-validation ticks;
  * class-sweep sibling: the ``wait_for_events`` long-poll re-checks
    liveness on its flag-recheck tick, so a token terminated mid-flight
    stops the wake loop instead of receiving event content for the rest
    of the window.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _operator_principal(project_name: str = "demo-project") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="test-operator",
        agent_id=None,
        sysadmin=False,
        project_name=project_name,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _new_pump():
    from agent_mcp.app.main_app import _McpAsgiApp

    # `_pump` never touches the manager; None keeps the test decoupled
    # from the StreamableHTTP wiring.
    return _McpAsgiApp(manager=None)


async def _start_pump(pump, session_id, queue, bearer, sent, *, heartbeat):
    pump._HEARTBEAT_INTERVAL_SECONDS = heartbeat

    async def send(message):
        sent.append(message)

    return asyncio.create_task(
        pump._pump(session_id, queue, send, bearer),
        name="test-mcp-pump",
    )


def _open_stream(session_registry, worker):
    session_id = session_registry.register_session(
        agent_id=worker.agent_id, bearer_token=worker.token,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    session_registry.attach_runtime_queue(session_id, queue)
    return session_id, queue


def _cleanup_stream(session_registry, session_id, pump_task):
    pump_task.cancel()
    session_registry.detach_runtime_queue(session_id)
    session_registry.unregister_session(session_id)


async def test_pump_self_validates_and_closes_on_revocation(tmp_path):
    """Self-validating pump: dropping the bearer from the auth cache
    (exactly what terminate's ``evict_from_cache`` does) tears the open
    stream down on the next heartbeat self-validation tick, with NO
    active nudge in play."""
    from agent_mcp.core import globals as g
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-revoke")
        session_id, queue = _open_stream(session_registry, worker)

        sent: list = []
        pump_task = await _start_pump(
            _new_pump(), session_id, queue, worker.token, sent,
            heartbeat=0.02,
        )
        try:
            # Live agent → stream stays open and emits heartbeats.
            await asyncio.sleep(0.1)
            assert not pump_task.done(), "live stream must stay open"
            assert any(
                b"heartbeat" in m.get("body", b"") for m in sent
            ), "expected at least one heartbeat frame while live"

            # Revoke — drop the bearer from the auth cache.
            g.active_agents.pop(worker.token, None)

            # Self-validation must break the pump without any nudge.
            await asyncio.wait_for(pump_task, timeout=2.0)
            assert pump_task.done()
        finally:
            _cleanup_stream(session_registry, session_id, pump_task)


async def test_terminate_agent_actively_closes_open_stream(tmp_path):
    """terminate_agent signals the open stream so it closes promptly.

    A deliberately long heartbeat means only the ACTIVE close-signal
    (not the periodic self-validation tick) can end the pump inside the
    test window — proving terminate reaches the stream, not just the
    eventual heartbeat backstop.
    """
    from agent_mcp.core import session_registry
    from agent_mcp.tools.admin_tools import terminate_agent_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-term")
        session_id, queue = _open_stream(session_registry, worker)

        sent: list = []
        pump_task = await _start_pump(
            _new_pump(), session_id, queue, worker.token, sent,
            heartbeat=30.0,
        )
        try:
            await asyncio.sleep(0.05)
            assert not pump_task.done()

            result = await terminate_agent_tool_impl(
                {"agent_id": worker.agent_id},
                principal=_operator_principal(),
            )
            assert isinstance(result, Ok), result

            await asyncio.wait_for(pump_task, timeout=2.0)
            assert pump_task.done(), (
                "terminate_agent must actively close the open stream"
            )
        finally:
            _cleanup_stream(session_registry, session_id, pump_task)


async def test_live_stream_keeps_pumping_across_validation_ticks(tmp_path):
    """Regression guard: an ACTIVE agent's stream stays open across many
    self-validation ticks and still delivers payloads."""
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-live")
        session_id, queue = _open_stream(session_registry, worker)

        sent: list = []
        pump_task = await _start_pump(
            _new_pump(), session_id, queue, worker.token, sent,
            heartbeat=0.02,
        )
        try:
            # Several validation ticks pass — pump must stay alive.
            await asyncio.sleep(0.15)
            assert not pump_task.done(), (
                "self-validation must not tear down a live stream"
            )

            # A real notification is delivered as a data: frame.
            queue.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated",
                    "params": {"uri": "agent-mcp://inbox/w-live"},
                }
            )
            await asyncio.sleep(0.05)
            assert any(
                m.get("body", b"").startswith(b"data: ") for m in sent
            ), "live payload must be delivered as a data: frame"
            assert not pump_task.done(), (
                "delivering a payload must not end a live stream"
            )
        finally:
            _cleanup_stream(session_registry, session_id, pump_task)


async def test_wait_for_events_stops_when_agent_terminated_midflight(
    tmp_path,
):
    """Class-sweep sibling (wait_for_events long-poll): the per-tick
    liveness re-check flips to disabled once the agent is terminated, so
    an in-flight long-poll returns ``stop_listening`` instead of
    continuing to deliver event content for the rest of its window."""
    from agent_mcp.tools.agent_communication_tools import (
        _check_auto_event_loop_flags,
    )
    from agent_mcp.tools.admin_tools import terminate_agent_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-longpoll")

        enabled, _ = _check_auto_event_loop_flags(worker.agent_id)
        assert enabled is True, "active agent's wake loop must be enabled"

        result = await terminate_agent_tool_impl(
            {"agent_id": worker.agent_id},
            principal=_operator_principal(),
        )
        assert isinstance(result, Ok), result

        enabled, reason = _check_auto_event_loop_flags(worker.agent_id)
        assert enabled is False, (
            "a terminated agent's wake loop must stop mid-flight"
        )
        assert "terminat" in (reason or "").lower(), reason
