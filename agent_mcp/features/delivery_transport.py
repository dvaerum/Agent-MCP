"""Delivery-transport registry — per-worker SSE subscriptions + status
(ADR-0021).

A runtime (e.g. the AoE bridge) opens one delivery stream per worker
(`GET /api/<project>/delivery/stream`, worker-bearer authed) and reports
that worker's session status (`POST .../delivery/status`). This in-process
hub holds, per ``agent_id``:

- the live SSE subscription(s) — frames pushed here reach the runtime,
  which injects them into the session;
- the last-reported ``transport-status`` ∈ {working, idle, dormant, dead},
  a SEPARATE signal from agent-mcp's own connection-presence (ADR-0021).

Modelled on :mod:`agent_mcp.features.operator_events`, but keyed by
``agent_id`` (one worker = one transport) and carrying status.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: The statuses a runtime may report (ADR-0021).
VALID_STATUSES: tuple[str, ...] = ("working", "idle", "dormant", "dead")


@dataclass(eq=False)
class Subscription:
    """One live delivery stream for a worker. ``eq=False`` so identity
    (not field values) keys removal, mirroring operator_events."""

    agent_id: str
    queue: "asyncio.Queue[Dict[str, Any]]"
    connected_at: str


# agent_id -> its live stream subscriptions (normally one; a list tolerates
# a brief overlap across a reconnect without dropping frames).
_subs: Dict[str, List[Subscription]] = {}
# agent_id -> last-reported transport-status.
_status: Dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def subscribe(agent_id: str) -> Subscription:
    """Register a live delivery stream for ``agent_id``."""
    sub = Subscription(
        agent_id=agent_id, queue=asyncio.Queue(), connected_at=_now_iso()
    )
    _subs.setdefault(agent_id, []).append(sub)
    return sub


def unsubscribe(sub: Subscription) -> None:
    """Drop a stream (on disconnect). A disconnect is NOT a status change —
    ``transport-status`` only changes via an explicit status report, so a
    transient drop doesn't flip a worker to ``dead`` (ADR-0021)."""
    subs = _subs.get(sub.agent_id)
    if not subs:
        return
    try:
        subs.remove(sub)
    except ValueError:
        pass
    if not subs:
        _subs.pop(sub.agent_id, None)


def push(agent_id: str, frame: Dict[str, Any]) -> int:
    """Enqueue ``frame`` onto every live stream for ``agent_id``. Returns
    the number of streams reached (0 = the worker has no live transport, so
    the frame is dropped — the fallback policy re-fires next cycle)."""
    subs = _subs.get(agent_id)
    if not subs:
        return 0
    n = 0
    for sub in list(subs):
        try:
            sub.queue.put_nowait(frame)
            n += 1
        except Exception:  # pragma: no cover - defensive
            pass
    return n


def is_connected(agent_id: str) -> bool:
    """True iff the worker has at least one live delivery stream."""
    return bool(_subs.get(agent_id))


def set_status(agent_id: str, status: str) -> None:
    _status[agent_id] = status


def get_status(agent_id: str) -> Optional[str]:
    """The worker's last-reported transport-status, or None if never
    reported."""
    return _status.get(agent_id)


def snapshot() -> List[Dict[str, Any]]:
    """Observability: one row per worker with a live stream and/or a
    reported status."""
    ids = set(_subs) | set(_status)
    rows: List[Dict[str, Any]] = []
    for agent_id in sorted(ids):
        subs = _subs.get(agent_id, [])
        rows.append(
            {
                "agent_id": agent_id,
                "connected": bool(subs),
                "streams": len(subs),
                "status": _status.get(agent_id),
            }
        )
    return rows


def clear() -> None:
    """Test hook — reset the registry."""
    _subs.clear()
    _status.clear()
