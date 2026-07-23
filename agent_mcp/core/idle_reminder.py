"""Idle backlog reminder for the ``wait_for_events`` wake loop.

When an agent is sitting idle in the event loop but still has unaddressed
work — unread messages and/or OPEN tasks assigned to it (status not
completed / cancelled / failed) — periodically wake it with a ``reminder``
event that lists exactly what's outstanding and tells it to go handle it.
No backlog → no reminder, so a genuinely-idle agent stays parked for free.

Cadence is a per-agent timer (in-memory; a restart just restarts the timer,
which for an hour-scale interval is harmless). It's seeded on first sight so
a freshly-connected agent isn't reminded immediately, and advanced every
time the reminder fires — whether or not there was a backlog to report — so
the check runs at most once per interval.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import logger

# Task statuses that are still "open" — a reminder nudges these. Anything
# else (completed / cancelled / failed) is terminal and left alone.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled", "canceled", "failed"})

# Cap how many items the reminder itemizes (the true totals are still
# reported); keeps a large backlog from ballooning the event.
_LIST_CAP = 15

# agent_id -> monotonic timestamp of the last reminder check.
_last_check: Dict[str, float] = {}


def seconds_until_due(agent_id: str, interval: float, now_mono: float) -> float:
    """Seconds until this agent's next reminder check. Seeds to ``now`` on
    first sight (so a fresh connection waits a full interval, not fires
    immediately)."""
    last = _last_check.get(agent_id)
    if last is None:
        _last_check[agent_id] = now_mono
        return interval
    return max(0.0, (last + interval) - now_mono)


def mark_checked(agent_id: str, now_mono: float) -> None:
    """Advance the timer (called every time the interval elapses, whether or
    not a reminder was actually sent)."""
    _last_check[agent_id] = now_mono


def clear() -> None:
    """Drop all timers (test isolation helper)."""
    _last_check.clear()


def _subject_of(row: Dict[str, Any]) -> str:
    subj = (row.get("subject") or "").strip()
    if subj:
        return subj
    body = (row.get("message_content") or "").strip().replace("\n", " ")
    return (body[:57] + "…") if len(body) > 58 else (body or "(no content)")


def collect_backlog(agent_id: str) -> Optional[Dict[str, Any]]:
    """Return the agent's outstanding work, or ``None`` when there is none.

    ``{unread_count, task_count, unread_messages: [...], open_tasks: [...]}``.
    Fully defensive — any DB error yields ``None`` (no reminder) rather than
    breaking the wait loop.
    """
    try:
        from ..repositories import message_repo
        from ..repositories.task_repository import get_tasks_by_agent_id

        unread_rows = message_repo.query(
            {"to": agent_id, "read": False, "limit": _LIST_CAP},
            oldest_first=True,
        )
        unread_count = message_repo.count_unread(agent_id)

        open_tasks: List[Dict[str, Any]] = [
            {
                "task_id": t.get("task_id"),
                "title": t.get("title") or "(untitled)",
                "status": t.get("status"),
            }
            for t in get_tasks_by_agent_id(agent_id, limit=200)
            if (t.get("status") or "").lower() not in _TERMINAL_TASK_STATUSES
        ]
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("idle-reminder backlog lookup failed for %s: %s", agent_id, e)
        return None

    if not unread_count and not open_tasks:
        return None

    return {
        "unread_count": unread_count,
        "task_count": len(open_tasks),
        "unread_messages": [
            {
                "message_id": r.get("message_id"),
                "sender_id": r.get("sender_id"),
                "subject": _subject_of(r),
            }
            for r in unread_rows
        ],
        "open_tasks": open_tasks[:_LIST_CAP],
    }


def _format_message(backlog: Dict[str, Any]) -> str:
    lines: List[str] = [
        "⏰ Reminder — you have unaddressed work sitting in your queue. "
        "Please handle it now."
    ]
    uc, tc = backlog["unread_count"], backlog["task_count"]
    msgs, tasks = backlog["unread_messages"], backlog["open_tasks"]

    if uc:
        lines.append(f"\nUnread messages ({uc}):")
        for m in msgs:
            lines.append(f"  • from {m['sender_id']}: {m['subject']}")
        if uc > len(msgs):
            lines.append(f"  … and {uc - len(msgs)} more")

    if tc:
        lines.append(f"\nOpen tasks ({tc}):")
        for t in tasks:
            lines.append(f"  • [{t['status']}] {t['title']} ({t['task_id']})")
        if tc > len(tasks):
            lines.append(f"  … and {tc - len(tasks)} more")

    lines.append(
        "\nGo address these now: call get_agent_messages to read the "
        "messages, and view_tasks / update_task_status to progress the tasks."
    )
    return "\n".join(lines)


def reminder_event(backlog: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ``reminder`` event (same envelope shape as every other
    wait_for_events event) carrying the count AND the itemized list."""
    import datetime

    return {
        "type": "reminder",
        "ref_id": None,
        "timestamp": datetime.datetime.now().isoformat(),
        "payload": {
            "message": _format_message(backlog),
            "unread_count": backlog["unread_count"],
            "task_count": backlog["task_count"],
            "unread_messages": backlog["unread_messages"],
            "open_tasks": backlog["open_tasks"],
        },
    }
