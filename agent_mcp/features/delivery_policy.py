"""Delivery-transport fallback policy — the pure decision brain (ADR-0021).

Given a worker's signals (unread messages, open tasks, unassigned tasks,
transport-status), the per-project config, and per-worker bookkeeping,
:func:`evaluate` decides whether to ping the worker's delivery transport
NOW and returns the advanced bookkeeping. It is intentionally pure — no
I/O, no wall clock (``now`` is passed in) — so the scheduler that drives it
(and its tests) fully control timing.

Key properties (ADR-0021):
- A ping NEVER mutates read/done state; the *condition* is the source of
  truth. The agent acting clears the condition, which disarms the policy.
- Escalating backoff while a condition stays unmet (widen, cap), reset on
  clear.
- Status gating: never deliver to a ``dead`` transport; suppress while
  ``working``; a ``dormant`` session is pinged only when ``wake_dormant``.
- ``cooldown_seconds`` is the floor under the backoff gap.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Optional

TransportStatus = Literal["working", "idle", "dormant", "dead"]


@dataclass(frozen=True)
class DeliveryPolicyConfig:
    """The per-project fallback policy (mirrors the ``config_delivery_*``
    settings in :mod:`agent_mcp.core.settings_schema`)."""

    enabled: bool
    on_unread_messages: bool
    on_unfinished_tasks: bool
    on_unassigned_tasks: bool
    backoff_initial_seconds: int
    backoff_max_seconds: int
    cooldown_seconds: int
    wake_dormant: bool


@dataclass(frozen=True)
class WorkerSignals:
    """A snapshot of the state the policy reasons over for one worker."""

    unread_messages: int
    open_tasks: int
    unassigned_tasks: int
    transport_status: TransportStatus


@dataclass(frozen=True)
class PingBookkeeping:
    """Per-worker ping state, carried across evaluations (persisted by the
    scheduler). Empty = disarmed / never pinged this arm."""

    armed_since: Optional[float] = None
    last_ping_at: Optional[float] = None
    ping_count: int = 0


@dataclass(frozen=True)
class PingDecision:
    should_ping: bool
    reason: str
    bookkeeping: PingBookkeeping
    #: Earliest ``now`` at which a ping could next fire (None when disarmed
    #: or the transport is dead) — a hint for the scheduler's next wake.
    next_eligible_at: Optional[float]


_DISARMED = PingBookkeeping()


def _active_reason(
    config: DeliveryPolicyConfig, signals: WorkerSignals
) -> str:
    """The highest-priority armed condition, or "" if none. Messages beat
    unfinished tasks beat unassigned tasks."""
    if config.on_unread_messages and signals.unread_messages > 0:
        return "unread_messages"
    if config.on_unfinished_tasks and signals.open_tasks > 0:
        return "unfinished_tasks"
    if config.on_unassigned_tasks and signals.unassigned_tasks > 0:
        return "unassigned_tasks"
    return ""


def _required_gap(config: DeliveryPolicyConfig, ping_count: int) -> float:
    """The minimum gap that must elapse AFTER the ``ping_count``-th ping
    before another may fire: initial × 2^(n-1), capped at max, floored at
    cooldown."""
    exp = max(0, ping_count - 1)
    backoff = config.backoff_initial_seconds * (2 ** exp)
    backoff = min(backoff, config.backoff_max_seconds)
    return float(max(backoff, config.cooldown_seconds))


def evaluate(
    config: DeliveryPolicyConfig,
    signals: WorkerSignals,
    bookkeeping: PingBookkeeping,
    now: float,
) -> PingDecision:
    """Decide whether to ping ``now`` and return the advanced bookkeeping."""
    if not config.enabled:
        return PingDecision(False, "", _DISARMED, None)

    reason = _active_reason(config, signals)
    if not reason:
        # No condition holds → disarm (resets backoff for the next arm).
        return PingDecision(False, "", _DISARMED, None)

    # Armed. Establish armed_since on first arm; keep it thereafter.
    armed_since = (
        bookkeeping.armed_since if bookkeeping.armed_since is not None else now
    )
    bk = replace(bookkeeping, armed_since=armed_since)

    # Status gating — armed, but delivery may be impossible or suppressed.
    status = signals.transport_status
    if status == "dead":
        return PingDecision(False, reason, bk, None)
    if status == "working":
        # Suppressed now; stays armed so it fires once the session is idle.
        return PingDecision(False, reason, bk, None)
    if status == "dormant" and not config.wake_dormant:
        return PingDecision(False, reason, bk, None)

    # Deliverable (idle, or dormant with wake_dormant).
    if bk.last_ping_at is None:
        # First ping of this arm fires immediately.
        fired = replace(bk, last_ping_at=now, ping_count=1)
        return PingDecision(
            True, reason, fired, now + _required_gap(config, 1)
        )

    gap = _required_gap(config, bk.ping_count)
    if now - bk.last_ping_at >= gap:
        fired = replace(bk, last_ping_at=now, ping_count=bk.ping_count + 1)
        return PingDecision(
            True, reason, fired, now + _required_gap(config, fired.ping_count)
        )

    # Armed + deliverable but still inside the backoff window.
    return PingDecision(False, reason, bk, bk.last_ping_at + gap)
