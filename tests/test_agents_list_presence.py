"""Wave 7 PR 2 — coordinator transition. The dashboard's agents
list ("Online" / "Offline" / "Pending" badge) and per-agent detail
panel ("Last MCP connection" timestamp) derive presence from
:mod:`agent_mcp.core.session_registry` instead of the legacy
spawn-lifecycle ``status`` column. The backend surfaces two new
fields on every agent row:

* ``online: bool`` — at least one MCP session for this agent's
  bearer is currently subscribed to a live ``/mcp`` stream (i.e. a
  runtime queue is attached for one of its session handles).
* ``last_mcp_connection: str | None`` — ISO-UTC ``last_seen_at`` of
  the most recent session this agent has opened in the current
  backend process; ``None`` when the agent has never opened a stream
  (the "PENDING — paste snippet" state).

The plan asks for three behavioural cases:

* Registered but no MCP session ⇒ ``online=False`` and
  ``last_mcp_connection is None``.  Dashboard renders **Pending**.
* Registered + WorkerSession active ⇒ ``online=True`` and
  ``last_mcp_connection`` set.  Dashboard renders **Online**.
* Registered + WorkerSession disconnected gracefully ⇒
  ``online=False`` and ``last_mcp_connection`` still set (the
  ``mcp_sessions`` row is gone, the last_seen_at history died with
  the row — so the agent flips back to **Pending** semantics until
  the next connection).  This file pins the explicit
  "no-stale-online-after-disconnect" invariant.

The harness's :class:`WorkerSession` doesn't open a real ``/mcp``
SSE stream — it drives tools through the registered framework
handlers and stamps ContextVars directly.  These tests therefore
simulate the wire-level open/close by calling
``session_registry.register_session`` /
``attach_runtime_queue`` / ``detach_runtime_queue`` /
``unregister_session`` directly — the same calls the real
transport handler makes from ``app/main_app.py``'s GET ``/mcp``
handler.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_pending_agent_has_no_presence(tmp_path) -> None:
    """A registered-but-never-connected agent surfaces as
    ``online=False`` and ``last_mcp_connection is None`` on both
    ``/api/agents`` and ``/api/all-data``. The dashboard renders
    these as the **Pending** badge.
    """
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice-pending")

        agents = admin.get("/api/agents").json()
        row = next(
            (a for a in agents if a["agent_id"] == "alice-pending"), None,
        )
        assert row is not None, agents
        assert row["online"] is False, row
        assert row["last_mcp_connection"] is None, row

        all_data = admin.get("/api/all-data").json()
        all_row = next(
            (
                a for a in all_data["agents"]
                if a["agent_id"] == "alice-pending"
            ),
            None,
        )
        assert all_row is not None, all_data
        assert all_row["online"] is False, all_row
        assert all_row["last_mcp_connection"] is None, all_row


async def test_active_session_marks_agent_online(tmp_path) -> None:
    """When an agent has an open MCP session AND a runtime queue
    attached, both endpoints surface ``online=True`` and
    ``last_mcp_connection`` is the row's ISO ``last_seen_at``.
    """
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice-online")

        # Same calls the real GET /mcp transport handler makes on
        # stream open. Without `attach_runtime_queue` the row
        # exists but presence reads as "offline" (the runtime queue
        # is the live signal — see _mcp_presence_for in routes.py).
        sid = session_registry.register_session(
            agent_id="alice-online",
            bearer_token=worker.token,
        )
        session_registry.attach_runtime_queue(sid, asyncio.Queue())

        try:
            row = next(
                (
                    a for a in admin.get("/api/agents").json()
                    if a["agent_id"] == "alice-online"
                ),
                None,
            )
            assert row is not None
            assert row["online"] is True, row
            assert row["last_mcp_connection"] is not None, row

            all_row = next(
                (
                    a for a in admin.get("/api/all-data").json()["agents"]
                    if a["agent_id"] == "alice-online"
                ),
                None,
            )
            assert all_row is not None
            assert all_row["online"] is True, all_row
            assert all_row["last_mcp_connection"] is not None, all_row
        finally:
            session_registry.detach_runtime_queue(sid)
            session_registry.unregister_session(sid)


async def test_disconnect_clears_online_status(tmp_path) -> None:
    """After a graceful disconnect (the transport calls
    ``unregister_session`` + ``detach_runtime_queue``) the agent
    must flip back to ``online=False``.  Catching the regression
    where stale runtime state leaves the badge stuck on **Online**
    after the SSE writer is gone.
    """
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("alice-flapping")

        sid = session_registry.register_session(
            agent_id="alice-flapping",
            bearer_token=worker.token,
        )
        session_registry.attach_runtime_queue(sid, asyncio.Queue())

        assert next(
            (
                a for a in admin.get("/api/agents").json()
                if a["agent_id"] == "alice-flapping"
            )
        )["online"] is True

        # Graceful close — same teardown the GET /mcp transport
        # handler runs in its `finally` block.
        session_registry.detach_runtime_queue(sid)
        session_registry.unregister_session(sid)

        row = next(
            (
                a for a in admin.get("/api/agents").json()
                if a["agent_id"] == "alice-flapping"
            ),
            None,
        )
        assert row is not None
        assert row["online"] is False, row
        # Wire contract: `unregister_session` deletes the row, so
        # there is no surviving last_seen_at to surface. The agent
        # flips back to the Pending shape until its next connection
        # — that's intentional: "last seen" is only meaningful while
        # the backend remembers the session existed.
        assert row["last_mcp_connection"] is None, row


async def test_row_without_runtime_queue_reads_offline(tmp_path) -> None:
    """If a session row exists but no runtime queue is attached
    (a previous backend process registered it; the client hasn't
    reconnected yet), the dashboard reads ``online=False`` but
    ``last_mcp_connection`` is populated. That's the **Offline**
    badge state.
    """
    from agent_mcp.core import session_registry

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice-stale")

        sid = session_registry.register_session(
            agent_id="alice-stale", bearer_token="stale-bearer",
        )
        # Intentionally no attach_runtime_queue — simulates the
        # "row survived a backend restart" case.

        try:
            row = next(
                (
                    a for a in admin.get("/api/agents").json()
                    if a["agent_id"] == "alice-stale"
                ),
                None,
            )
            assert row is not None
            assert row["online"] is False, row
            assert row["last_mcp_connection"] is not None, row
        finally:
            session_registry.unregister_session(sid)


async def test_parked_wait_for_events_marks_agent_online(tmp_path) -> None:
    """Task #360 regression. An agent parked in ``wait_for_events`` (a
    live long-poll, the primary event-loop channel) must read
    ``online=True`` — even with NO GET ``/mcp`` SSE runtime queue.

    Before the fix, presence was derived ONLY from
    ``session_registry`` (the SSE stream), which a parked long-poll
    never touches, so an actively-listening agent showed a misleading
    **Offline**. The in-memory waiter registry
    (``state.register_waiter`` on poll START) is the authoritative,
    zero-persistence signal for "currently holding an event-loop
    connection", so presence ORs it in.
    """
    from agent_mcp.core import state

    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice-parked")

        # Simulate what the real wait_for_events does on entry.
        q = state.register_waiter("alice-parked")
        try:
            row = next(
                (
                    a for a in admin.get("/api/agents").json()
                    if a["agent_id"] == "alice-parked"
                ),
                None,
            )
            assert row is not None, "agent row missing"
            assert row["online"] is True, row  # RED before the fix

            all_row = next(
                (
                    a for a in admin.get("/api/all-data").json()["agents"]
                    if a["agent_id"] == "alice-parked"
                ),
                None,
            )
            assert all_row is not None
            assert all_row["online"] is True, all_row
        finally:
            state.unregister_waiter("alice-parked", q)

        # Park ended → no live connection of either kind → not online.
        row2 = next(
            (
                a for a in admin.get("/api/agents").json()
                if a["agent_id"] == "alice-parked"
            ),
            None,
        )
        assert row2 is not None
        assert row2["online"] is False, row2
