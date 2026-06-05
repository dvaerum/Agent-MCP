# Agent-MCP/agent_mcp/core/event_bus.py
"""EventBus — single named seam for agent state notifications (PR-W2b).

Mutators (`send_agent_message`, `broadcast_admin_message`, the various
`assign_task_*` paths, `update_task_status`, `_create_unassigned_tasks`)
all need to wake the same downstream consumers when something happens
to an agent's inbox. Before this module existed, each writer
called ``g.notify_agent_inbox(agent_id)``, a single function whose body
hard-coded two fanout paths (long-poll ``asyncio.Event`` + streaming
queue) wrapped in twin try/except blocks.

The bus is the same wake operation expressed as a typed registry: each
sink is an :class:`EventBusAdapter`, registered by name at module
import. ``bus.notify(agent_id, event_type, payload)`` walks the
registry and invokes ``deliver`` on each adapter, catching per-adapter
exceptions so a misbehaving sink can never poison the writer or the
other sinks. New sinks (audit log, metrics, future Redis pubsub) just
implement the Protocol and call :func:`register`.

The legacy shims ``state.notify_agent_inbox(agent_id)`` and
``state.notify_unassigned_task_appeared(task_id, capabilities)`` now
route through ``bus.notify``. Their signatures and exception-tolerance
contracts are unchanged so every existing call site keeps working
without edits; callsite migrations to ``bus.notify(...)`` happen in
Wave-2c (#6 Repositories) or a follow-up PR.

Failure model: writers have already committed to SQLite by the time
they call into the bus. Notifications are side-effects on transient
in-process state (the long-poll Event, the per-session queue) — best
effort is correct. The per-adapter try/except logs a warning and
moves on; readers rely on the committed DB row, not the wake edge.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


logger = logging.getLogger("mcp_server.event_bus")


# ---------------------------------------------------------------------------
# Adapter protocol + registry
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBusAdapter(Protocol):
    """Anything with a ``deliver(agent_id, event_type, payload)`` method.

    Use ``@runtime_checkable`` so tests can ``isinstance(x,
    EventBusAdapter)`` against duck-typed fakes without inheriting.
    """

    def deliver(
        self,
        agent_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]],
    ) -> None: ...


# Ordered (name, adapter) pairs — name is used for unregister() and
# for the warning log when an adapter crashes. A list preserves
# registration order so failure messages identify the offender
# deterministically across runs.
_adapters: List[Tuple[str, EventBusAdapter]] = []


def register(name: str, adapter: EventBusAdapter) -> None:
    """Register ``adapter`` under ``name``. Replaces any prior adapter
    with the same name (idempotent — useful for test re-registration).
    """
    unregister(name)
    _adapters.append((name, adapter))


def unregister(name: str) -> None:
    """Remove the adapter with the given ``name`` from the registry.

    No-op if no adapter is registered under ``name``. Used by tests
    that register a spy and need to clean up after themselves.
    """
    global _adapters
    _adapters = [(n, a) for (n, a) in _adapters if n != name]


def notify(
    agent_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Fan out ``(agent_id, event_type, payload)`` to every adapter.

    Per-adapter exceptions are caught and logged at WARNING; they
    never propagate. This protects callers (which have already
    committed their source-of-truth write) from notification
    side-effect failures.
    """
    for name, adapter in _adapters:
        try:
            adapter.deliver(agent_id, event_type, payload)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch
            logger.warning(
                "EventBus adapter %r crashed delivering %s/%s: %s",
                name,
                agent_id,
                event_type,
                exc,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Default adapters
# ---------------------------------------------------------------------------
#
# The three adapters below preserve the pre-PR-W2b behavior of
# ``state.notify_agent_inbox``:
#   * LongPollSignalAdapter wakes any in-process ``wait_for_events``
#     blocked on ``state.signal_for(agent_id)``. Synthetic event types
#     (those that don't have their own DB row — currently just
#     ``unassigned_task_appeared``) also get pushed onto the per-agent
#     out-of-band queue so the wake'd waiter has something concrete to
#     return.
#   * StreamingQueueAdapter pushes a JSON-RPC
#     ``notifications/resources/updated`` envelope onto every GET /mcp
#     streaming queue registered for the agent. The "agent_inbox"
#     event_type preserves the original ``agent-mcp://inbox/<id>`` URI
#     contract; other event types get a typed URI so subscribers can
#     route without sniffing payload shapes.
#   * AuditLogAdapter is a no-op-by-default DEBUG sink, env-gated by
#     ``AGENT_MCP_EVENT_BUS_AUDIT=1``. Its job in this PR is to prove
#     the registry pattern handles arbitrary adapters; future PRs can
#     swap in real audit/metrics sinks via the same seam.


# Event types that don't have their own DB row and need the long-poll
# waiter's per-agent out-of-band queue (drained on wake) to carry the
# payload. The DB-backed event types (``agent_inbox`` umbrella event,
# task changes, messages) only need the wake-edge — the impl re-queries
# SQLite to assemble the response envelope.
_SYNTHETIC_EVENT_TYPES = frozenset({"unassigned_task_appeared"})


class LongPollSignalAdapter:
    """Wakes ``wait_for_events`` waiters via ``state.signal_for``.

    Mirrors the pre-PR-W2b behavior:
      * Always ``state.signal_for(agent_id).set()`` so any in-flight
        waiter wakes within one event-loop iteration.
      * For synthetic event types, also append a skinny event dict to
        the per-agent out-of-band queue so the waiter's drain pass on
        wake sees the new event (it can't re-query a DB row that
        never existed).
    """

    def deliver(
        self,
        agent_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        # Late import to avoid a circular dependency at module load —
        # state.py is already importable here, but explicit late-bind
        # keeps the import graph shallow and matches the pattern used
        # by the legacy notify_agent_inbox.
        from . import state

        if event_type in _SYNTHETIC_EVENT_TYPES:
            # Preserve the legacy queued-event shape
            # ``{type, ref_id, timestamp, payload}`` that
            # ``wait_for_events_tool_impl`` drains via
            # ``state.drain_events``. ``ref_id`` and ``timestamp`` ride
            # on the payload dict from the writer (see
            # ``state.notify_unassigned_task_appeared``) so the bus
            # interface stays a flat ``(agent_id, event_type,
            # payload)`` triple.
            data = payload or {}
            try:
                state.push_event(
                    agent_id,
                    {
                        "type": event_type,
                        "ref_id": data.get("ref_id"),
                        "timestamp": data.get("timestamp"),
                        "payload": data,
                    },
                )
            except Exception:  # pragma: no cover — defensive
                # The signal_for wake below still fires so the waiter
                # has a chance to re-query the DB.
                pass

        try:
            state.signal_for(agent_id).set()
        except Exception:  # pragma: no cover — defensive
            pass


class StreamingQueueAdapter:
    """Pushes JSON-RPC notification payloads to GET /mcp subscribers.

    ``session_registry.fanout_to_agent`` already handles the no-active-
    subscriber case (returns empty list) and the queue-full case (logs
    + drops). We only translate ``(event_type, payload)`` into the
    wire-format envelope.

    For ``"agent_inbox"`` we keep the existing URI
    (``agent-mcp://inbox/<agent_id>``) so dashboard subscribers that
    were written against the pre-PR-W2b shape don't need to change.
    Future event types get a typed URI carrying the event_type, which
    lets subscribers route without parsing the payload.
    """

    def deliver(
        self,
        agent_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        from . import session_registry

        if event_type == "agent_inbox":
            uri = f"agent-mcp://inbox/{agent_id}"
            envelope: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri},
            }
        else:
            uri = f"agent-mcp://events/{agent_id}/{event_type}"
            envelope = {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {
                    "uri": uri,
                    "event_type": event_type,
                    "payload": payload or {},
                },
            }
        try:
            session_registry.fanout_to_agent(agent_id, envelope)
        except Exception:  # pragma: no cover — defensive
            pass


class AuditLogAdapter:
    """Best-effort DEBUG audit log for the EventBus.

    Disabled by default — set ``AGENT_MCP_EVENT_BUS_AUDIT=1`` in the
    environment to turn it on for local debugging. Even when enabled,
    failures inside the adapter are caught by the bus, so a broken
    audit sink never affects mutator latency.
    """

    def __init__(self) -> None:
        self._enabled = os.environ.get("AGENT_MCP_EVENT_BUS_AUDIT") == "1"

    def deliver(
        self,
        agent_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]],
    ) -> None:
        if not self._enabled:
            return
        # Keep the log line short — agent_id + event_type is enough to
        # correlate with the wait_for_events impl logs; payload sizes
        # vary wildly so we only log a key count.
        payload_size = 0 if payload is None else len(payload)
        logger.debug(
            "EventBus audit: agent_id=%s event_type=%s payload_keys=%d",
            agent_id,
            event_type,
            payload_size,
        )


# Register default adapters at import time. Order matters for
# determinism in the audit log: long-poll fires first (so an in-process
# waiter wakes before the streaming subscriber sees the wire payload),
# streaming next, audit last (never a no-op-blocker).
register("LongPollSignalAdapter", LongPollSignalAdapter())
register("StreamingQueueAdapter", StreamingQueueAdapter())
register("AuditLogAdapter", AuditLogAdapter())
