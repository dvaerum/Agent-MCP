"""Adaptive hold ladder for the ``wait_for_events`` long-poll.

A heartbeat-capable client (Claude Code / OpenCode, or any client that
sends a ``progressToken``) can have its connection parked open indefinitely
— the server keeps it alive with heartbeats and returns the instant a real
event arrives, so an idle wait costs the agent nothing. Some agents instead
cap themselves with a short ``timeout_seconds`` and re-poll, burning a model
turn on every empty return. The tool schema says to omit the timeout, but an
agent may ignore that.

This module tracks, per agent, a run of *consecutive empty short-polls* (a
poll that passed a timeout SHORTER than the parked hold and came back with
nothing) and escalates:

* below :data:`ADVISE_AFTER` — leave it alone. A one-off long monitor (a
  single big timeout) never trips this; only a run of short empty polls does.
* :data:`ADVISE_AFTER` … :data:`OVERRIDE_AFTER` — ADVISE: return an escalating
  ``hold_advisory`` event telling the agent to drop the timeout.
* at/after :data:`OVERRIDE_AFTER` — OVERRIDE: ignore the agent's short cap and
  park the connection anyway, until a real event arrives.

The run resets the moment a real event is delivered, or when the agent stops
capping itself (omits the timeout). State is in-memory per ``agent_id`` — a
backend restart just resets the ladders, which is harmless.

Only ever applied by the caller when the client is heartbeat-capable AND sent
a progressToken: without heartbeats a long silent hold would let the client's
own idle watchdog kill the connection, which is worse than the short cap.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Consecutive empty short-polls before each step. Tunable.
ADVISE_AFTER = 20    # start telling the agent to stop capping itself
OVERRIDE_AFTER = 30  # start ignoring the cap and parking the connection

# agent_id -> current run length of consecutive empty short-polls.
_counts: Dict[str, int] = {}


def get_count(agent_id: str) -> int:
    return _counts.get(agent_id, 0)


def note_empty_short_poll(agent_id: str) -> int:
    """Record one empty short-poll for ``agent_id``; return the new run length."""
    _counts[agent_id] = _counts.get(agent_id, 0) + 1
    return _counts[agent_id]


def reset(agent_id: str) -> None:
    """Clear the run (a real event landed, or the agent stopped capping)."""
    _counts.pop(agent_id, None)


def clear() -> None:
    """Drop all ladder state (test isolation helper)."""
    _counts.clear()


@dataclass(frozen=True)
class LadderDecision:
    """What to do for one ``wait_for_events`` call given the run length."""

    phase: str            # "normal" | "advise" | "override"
    override_hold: bool   # ignore the caller's short timeout, park instead
    advisory: Optional[str]  # escalating text to return as a hold_advisory event


def decide(count: int) -> LadderDecision:
    if count >= OVERRIDE_AFTER:
        return LadderDecision("override", True, None)
    if count >= ADVISE_AFTER:
        return LadderDecision("advise", False, _advise_text(count))
    return LadderDecision("normal", False, None)


_BASE_ADVICE = (
    "You keep calling wait_for_events with a short timeout_seconds while "
    "nothing is waiting. You do NOT need to poll: this connection is held "
    "open and kept alive with heartbeats, and it returns the instant a real "
    "event (message, task, or directive) arrives. Drop timeout_seconds "
    "entirely and just call wait_for_events() — an idle wait then costs you "
    "nothing."
)


def _advise_text(count: int) -> str:
    """Escalating advisory: gentle at first, a countdown as it nears the
    override, and a final notice on the last step before the server takes
    over the hold."""
    remaining = OVERRIDE_AFTER - count
    if remaining <= 1:
        return (
            _BASE_ADVICE + " FINAL NOTICE: from your next call on I will hold "
            "your connection open regardless of the timeout you pass, until a "
            "real event arrives."
        )
    if count >= ADVISE_AFTER + (OVERRIDE_AFTER - ADVISE_AFTER) // 2:
        return (
            f"Reminder ({remaining} more short empty polls and I will override "
            f"your timeout and hold the connection open myself): " + _BASE_ADVICE
        )
    return _BASE_ADVICE


def advisory_event(message: str) -> Dict[str, Any]:
    """A synthetic ``hold_advisory`` event, shaped like every other event in
    the wait_for_events envelope so the agent handles it uniformly."""
    return {
        "type": "hold_advisory",
        "ref_id": None,
        "timestamp": datetime.datetime.now().isoformat(),
        "payload": {"message": message},
    }
