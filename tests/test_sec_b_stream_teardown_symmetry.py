"""SEC-B [stream-teardown-symmetry]: two gaps in the AC-R29-1 revoke
completeness story (see ``test_sec_r29_terminate_sse_revoke.py``).

  * F2 — the GET /mcp pump re-validates the bearer at the TOP of its
    loop, but a data payload queued BEFORE revocation can already be
    sitting in front of the ``CLOSE_STREAM`` sentinel when the pump
    wakes up. Before this fix the pump would dequeue that payload,
    see it isn't the sentinel, and wire-write it to the now-revoked
    bearer — one payload slips through before the *next* iteration's
    top-of-loop check finally tears the stream down. The fix
    re-checks the same liveness predicate AFTER dequeue and BEFORE
    send, discarding the payload and tearing down immediately if the
    bearer left ``active_agents`` in between.

  * F3 — ``terminate_agent`` actively signals open streams to close
    (``session_registry.close_streams_for_agent``) so revocation is
    immediate rather than waiting for the next heartbeat. ``purge_agent``
    evicts the same ``active_agents`` cache entry but never made that
    same call, so a purged LIVE agent's stream lingered up to one
    heartbeat interval before self-validating shut. The fix adds the
    same active signal to purge's post-commit hook.

Invariant pinned by both tests: any operation that changes
``active_agents`` membership tears down its streams, and the pump never
wire-writes to a bearer that has left ``active_agents``.
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


async def test_pump_does_not_send_payload_after_revoke(tmp_path):
    """F2: a payload already in flight when revocation happens must not
    reach the wire, even though it isn't the ``CLOSE_STREAM`` sentinel.

    A long heartbeat means the top-of-loop self-validation tick can't
    fire inside the test window — the pump must already be blocked in
    ``queue.get()`` (having passed the top check while the bearer was
    still live) when we revoke and then deliver the queued payload. The
    only thing that can stop it from sending is the post-dequeue,
    pre-send re-check this fix adds.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-race")
        session_id, queue = _open_stream(session_registry, worker)

        sent: list = []
        pump_task = await _start_pump(
            _new_pump(), session_id, queue, worker.token, sent,
            heartbeat=30.0,
        )
        try:
            # Let the pump pass its top-of-loop liveness check (bearer
            # still live) and block in `queue.get()`.
            await asyncio.sleep(0.05)
            assert not pump_task.done()

            # Revoke, THEN deliver the already-in-flight payload followed
            # by the sentinel — mirrors a data payload queued before
            # revocation with CLOSE_STREAM appended behind it.
            g.active_agents.pop(worker.token, None)
            queue.put_nowait(
                {"jsonrpc": "2.0", "method": "notifications/resources/updated"}
            )
            queue.put_nowait(session_registry.CLOSE_STREAM)

            await asyncio.wait_for(pump_task, timeout=2.0)
            assert pump_task.done()
            assert not any(
                m.get("body", b"").startswith(b"data: ") for m in sent
            ), "payload queued before revocation must not be sent"
        finally:
            _cleanup_stream(session_registry, session_id, pump_task)


async def test_purge_closes_streams_immediately(tmp_path):
    """F3: purge_agent, like terminate_agent, must actively signal an
    open GET /mcp stream to close rather than leaving it to the next
    heartbeat self-validation tick.

    A deliberately long heartbeat means only the ACTIVE close-signal
    (not the periodic self-validation tick) can end the pump inside the
    test window.
    """
    from agent_mcp.core import session_registry
    from agent_mcp.tools.admin_tools import purge_agent_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w-purge")
        session_id, queue = _open_stream(session_registry, worker)

        sent: list = []
        pump_task = await _start_pump(
            _new_pump(), session_id, queue, worker.token, sent,
            heartbeat=30.0,
        )
        try:
            await asyncio.sleep(0.05)
            assert not pump_task.done()

            result = await purge_agent_tool_impl(
                {"agent_id": worker.agent_id},
                principal=_operator_principal(),
            )
            assert isinstance(result, Ok), result

            await asyncio.wait_for(pump_task, timeout=2.0)
            assert pump_task.done(), (
                "purge_agent must actively close the open stream"
            )
        finally:
            _cleanup_stream(session_registry, session_id, pump_task)
