"""Render an agent-mcp delivery frame into the SKINNY nudge text injected
into a session (ADR-0021).

A frame carries only ids/subjects/status — never bodies — and this keeps it
that way: the text names what arrived and tells the agent which tool to
call to act on it. No message body is ever emitted.
"""

from __future__ import annotations

from typing import Any, Dict, List

# How many items to list before summarising the rest (keeps an injected
# nudge short even for a large backlog).
_LIST_CAP = 5


def _msg_lines(messages: List[Dict[str, Any]]) -> List[str]:
    out = []
    for m in messages[:_LIST_CAP]:
        subject = m.get("subject") or "(no subject)"
        sender = m.get("sender_id") or "?"
        mid = m.get("message_id") or "?"
        out.append(f"  • {subject} — from {sender} [{mid}]")
    if len(messages) > _LIST_CAP:
        out.append(f"  • …and {len(messages) - _LIST_CAP} more")
    return out


def _task_lines(tasks: List[Dict[str, Any]]) -> List[str]:
    out = []
    for t in tasks[:_LIST_CAP]:
        title = t.get("title") or "(untitled)"
        status = t.get("status") or "?"
        tid = t.get("task_id") or "?"
        out.append(f"  • {title} — {status} [{tid}]")
    if len(tasks) > _LIST_CAP:
        out.append(f"  • …and {len(tasks) - _LIST_CAP} more")
    return out


def render_frame(frame: Dict[str, Any]) -> str:
    """Return the skinny nudge text for a delivery ``frame``.

    The frame shape is produced by
    ``agent_mcp.features.delivery_scheduler._render_frame``:
    ``{type, reason, unread_count, task_count, unread_messages[],
    open_tasks[], (unassigned_count)}``.
    """
    reason = frame.get("reason")

    if reason == "unread_messages":
        n = frame.get("unread_count", 0)
        head = (
            f"📨 agent-mcp: you have {n} unread message"
            f"{'s' if n != 1 else ''} — call get_agent_messages to read."
        )
        lines = _msg_lines(frame.get("unread_messages", []))
        return "\n".join([head, *lines]) if lines else head

    if reason == "unfinished_tasks":
        n = frame.get("task_count", 0)
        head = (
            f"📋 agent-mcp: you have {n} open task"
            f"{'s' if n != 1 else ''} — call view_tasks / update as you go."
        )
        lines = _task_lines(frame.get("open_tasks", []))
        return "\n".join([head, *lines]) if lines else head

    if reason == "unassigned_tasks":
        n = frame.get("unassigned_count", 0)
        return (
            f"🗂️ agent-mcp: {n} unassigned task"
            f"{'s' if n != 1 else ''} in the pool you could claim — "
            "call view_tasks."
        )

    # Unknown reason: a minimal, safe pointer (never dumps unknown content).
    return "🔔 agent-mcp: you have pending items — check your inbox / tasks."
