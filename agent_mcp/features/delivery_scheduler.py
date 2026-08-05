"""Delivery scheduler — drives the fallback policy and pushes frames
(ADR-0021).

Ties the three pieces together: read the per-project policy config, read
each connected worker's signals (unread messages / open tasks / unassigned
tasks + reported transport-status), run the pure
:mod:`~agent_mcp.features.delivery_policy` engine, and — when it says ping —
render a **skinny** frame (ids/titles/status, never bodies) and push it down
the worker's delivery stream.

Per-worker bookkeeping (backoff/cooldown state) lives in this process; a
frame that can't be delivered (no live stream) is simply dropped and the
policy re-fires next tick (self-healing, ADR-0021).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from ..core import idle_reminder
from ..tools.access import _get_config_bool, _get_config_int
from . import delivery_policy as dp
from . import delivery_transport

logger = logging.getLogger("mcp_server")

_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "cancelled", "canceled", "failed"}
)

# Per-worker ping bookkeeping for THIS backend process.
_bookkeeping: Dict[str, dp.PingBookkeeping] = {}

#: How often the background loop re-evaluates (a poll floor; the policy's
#: own backoff decides whether a tick actually pings).
TICK_INTERVAL_SECONDS = 15


def load_config() -> dp.DeliveryPolicyConfig:
    """Resolve the per-project fallback policy from ``project_settings``."""
    return dp.DeliveryPolicyConfig(
        enabled=_get_config_bool("config_delivery_enabled", False),
        on_unread_messages=_get_config_bool(
            "config_delivery_on_unread_messages", True
        ),
        on_unfinished_tasks=_get_config_bool(
            "config_delivery_on_unfinished_tasks", True
        ),
        on_unassigned_tasks=_get_config_bool(
            "config_delivery_on_unassigned_tasks", False
        ),
        backoff_initial_seconds=_get_config_int(
            "config_delivery_backoff_initial_seconds", 30
        ),
        backoff_max_seconds=_get_config_int(
            "config_delivery_backoff_max_seconds", 3600
        ),
        cooldown_seconds=_get_config_int(
            "config_delivery_cooldown_seconds", 60
        ),
        wake_dormant=_get_config_bool("config_delivery_wake_dormant", False),
    )


def _count_unassigned_open() -> int:
    """Open tasks in the pool with no assignee (project-wide)."""
    try:
        from ..repositories.task_repository import get_all_tasks_from_db

        return sum(
            1
            for t in get_all_tasks_from_db()
            if not (t.get("assigned_to") or "")
            and (t.get("status") or "").lower() not in _TERMINAL_TASK_STATUSES
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("delivery: unassigned-count lookup failed: %s", e)
        return 0


def _signals_for(
    agent_id: str,
    backlog: Optional[Dict[str, Any]],
    config: dp.DeliveryPolicyConfig,
) -> dp.WorkerSignals:
    unread = int(backlog["unread_count"]) if backlog else 0
    open_tasks = int(backlog["task_count"]) if backlog else 0
    # Only pay the project-wide scan when the trigger is armed.
    unassigned = _count_unassigned_open() if config.on_unassigned_tasks else 0
    # A connected worker that hasn't reported status yet is treated as idle
    # (deliverable); an explicit report overrides.
    status = delivery_transport.get_status(agent_id) or "idle"
    return dp.WorkerSignals(
        unread_messages=unread,
        open_tasks=open_tasks,
        unassigned_tasks=unassigned,
        transport_status=status,  # type: ignore[arg-type]
    )


def _render_frame(
    reason: str,
    backlog: Optional[Dict[str, Any]],
    unassigned_count: int,
) -> Dict[str, Any]:
    """A SKINNY frame — ids/subjects/status only, never message bodies
    (ADR-0021). Mirrors what the event loop would have delivered."""
    frame: Dict[str, Any] = {
        "type": "delivery",
        "reason": reason,
        "unread_count": backlog["unread_count"] if backlog else 0,
        "task_count": backlog["task_count"] if backlog else 0,
        "unread_messages": backlog["unread_messages"] if backlog else [],
        "open_tasks": backlog["open_tasks"] if backlog else [],
    }
    if reason == "unassigned_tasks":
        frame["unassigned_count"] = unassigned_count
    return frame


def evaluate_and_push(
    agent_id: str,
    config: dp.DeliveryPolicyConfig,
    now: float,
) -> bool:
    """Evaluate one worker and push a frame iff the policy says ping.
    Returns whether a frame was pushed. Advances the worker's bookkeeping."""
    backlog = idle_reminder.collect_backlog(agent_id)
    signals = _signals_for(agent_id, backlog, config)
    bk = _bookkeeping.get(agent_id, dp.PingBookkeeping())
    decision = dp.evaluate(config, signals, bk, now)
    _bookkeeping[agent_id] = decision.bookkeeping
    if not decision.should_ping:
        return False
    frame = _render_frame(decision.reason, backlog, signals.unassigned_tasks)
    delivery_transport.push(agent_id, frame)
    return True


def tick(now: Optional[float] = None) -> int:
    """One scheduler pass over every connected worker. Returns the number
    of frames pushed. A no-op (no config read cost beyond the toggle) when
    the feature is disabled."""
    config = load_config()
    if not config.enabled:
        return 0
    if now is None:
        now = time.monotonic()
    pushed = 0
    for agent_id in delivery_transport.connected_agent_ids():
        try:
            if evaluate_and_push(agent_id, config, now):
                pushed += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("delivery: evaluate failed for %s: %s", agent_id, e)
    return pushed


async def run_loop(interval: float = TICK_INTERVAL_SECONDS) -> None:
    """Background loop — tick every ``interval`` seconds until cancelled."""
    logger.info("delivery scheduler loop started (interval=%ss)", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                tick()
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("delivery: tick failed: %s", e)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        logger.info("delivery scheduler loop stopped")
        raise


def clear() -> None:
    """Test hook — reset per-worker bookkeeping."""
    _bookkeeping.clear()
