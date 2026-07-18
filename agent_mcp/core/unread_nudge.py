"""Ambient unread-message nudge for agent callers.

Agents work through their normal MCP tool loop and never poll their
inbox; a message sent to them can sit unread indefinitely. Rather than
add a polling loop, we piggy-back on the traffic the agent already
generates: whenever an *agent bearer* makes any tool call and has unread
messages, we append ONE advisory line to that tool's response text —

    📬 You have 3 unread message(s) (from: manager, ios-app-dev).
    Call `get_agent_messages` to read them (reading marks them read).

The agent sees the line while doing its work, calls ``get_agent_messages``
(which already marks fetched messages read), the unread count drops to 0,
and the line stops appearing on the next call. No polling, no new wire
verb — the nudge is ambient.

Design constraints (all enforced here):

* **Agents only.** Gated on ``principal.kind == "agent_bearer"``.
  Operators (cookie / forwarding-header) and unauthenticated callers
  never see it — this is the MCP/agent wire only. The REST/dashboard
  path renders ``ToolResult`` through ``tool_result_to_http`` and never
  calls this function, so it is structurally excluded.
* **Checkpoint tools only.** The nudge fires only when the called tool is
  in :data:`_UNREAD_NUDGE_TOOLS` — the curated "pause to decide what to do
  next" moments (see that constant). Inner-loop noise (file read/write,
  RAG queries, etc.) and the ``get_agent_messages`` read tool are NOT in
  the set, so they carry no nudge.
* **Only when unread > 0.** One cheap COUNT query
  (:func:`count_unread_for_recipient`, hitting the
  ``idx_agent_messages_unread`` covering index); the nudge is skipped
  entirely at zero.
* **Advisory only.** Additive: a new ``TextContent`` block is appended;
  the existing result blocks (prose + JSON payload) are never mutated, so
  the actual tool data is untouched.
* **Fail-safe.** Any error in the unread lookup is swallowed at debug and
  the original response returned unchanged. The nudge must never break a
  tool call.
"""

from __future__ import annotations

from typing import List, Optional

import mcp.types as mcp_types

from .config import logger
from .principal import Principal

# The tunable "checkpoint" set — the tools that represent a natural pause
# where an agent decides what to do next, so surfacing unread mail there is
# well-timed rather than noisy. Edit this ONE constant to tune where the
# nudge appears. Deliberately EXCLUDES the get-messages read tool (nudging
# there is redundant + racy — reading marks messages read) and all
# inner-loop tools (file I/O, RAG lookups) whose high call frequency would
# make the nudge spam.
#   * wait_for_events    — the coordination wait/loop tool; agents in a
#                          loop hit it constantly, so it's the prime moment.
#   * view_tasks         — surveying work: a decision point.
#   * create_task /
#     assign_task /
#     update_task_status — task-lifecycle mutations: the agent is actively
#                          steering work and should factor in fresh mail.
#   * send_agent_message — the agent is already thinking about coordination.
_UNREAD_NUDGE_TOOLS = frozenset(
    {
        "wait_for_events",
        "view_tasks",
        "create_task",
        "assign_task",
        "update_task_status",
        "send_agent_message",
    }
)

# Up to this many distinct senders are named in the advisory line.
_MAX_SENDERS = 3


def _format_nudge(unread: int, senders: List[str]) -> str:
    """Render the one-line advisory. Names senders when we have any,
    otherwise falls back to a count-only line (senders are best-effort).
    """
    read_hint = "Call `get_agent_messages` to read them (reading marks them read)."
    if senders:
        return (
            f"\U0001F4EC You have {unread} unread message(s) "
            f"(from: {', '.join(senders)}). {read_hint}"
        )
    return f"\U0001F4EC You have {unread} unread message(s). {read_hint}"


def maybe_append_unread_nudge(
    content: List[mcp_types.TextContent],
    *,
    principal: Optional[Principal],
    tool_name: str,
) -> List[mcp_types.TextContent]:
    """Return ``content`` with an unread-messages advisory appended when the
    caller is an agent bearer that has unread messages.

    Returns ``content`` unchanged (never raises) when the caller is not an
    agent bearer, when the called tool is not a checkpoint tool
    (:data:`_UNREAD_NUDGE_TOOLS`), when there are no unread messages, or
    when the unread lookup fails. See the module docstring for the full
    contract.
    """
    # Agent-only gate — operators / forwarding-header / unauthenticated
    # callers never see the nudge.
    if principal is None or principal.kind != "agent_bearer":
        return content
    agent_id = principal.agent_id
    if not agent_id:
        return content
    # Only the curated checkpoint tools carry the nudge; everything else
    # (inner-loop noise + the get-messages read tool) is skipped.
    if tool_name not in _UNREAD_NUDGE_TOOLS:
        return content

    try:
        from ..repositories.message_repository import count_unread_for_recipient

        unread = count_unread_for_recipient(agent_id)
        if unread <= 0:
            return content
        senders = _distinct_senders(agent_id)
        line = _format_nudge(unread, senders)
    except Exception:  # noqa: BLE001 - a nudge must never break a tool call
        logger.debug(
            "unread-nudge lookup failed for %r; returning response unchanged",
            agent_id,
            exc_info=True,
        )
        return content

    return [*content, mcp_types.TextContent(type="text", text=line)]


def _distinct_senders(agent_id: str) -> List[str]:
    """Best-effort distinct senders for the advisory line. Isolated so a
    senders-query failure degrades to a count-only nudge rather than
    dropping the nudge entirely (the count is the load-bearing part).
    """
    try:
        from ..repositories.message_repository import (
            distinct_unread_senders_for_recipient,
        )

        return distinct_unread_senders_for_recipient(agent_id, limit=_MAX_SENDERS)
    except Exception:  # noqa: BLE001 - senders are best-effort
        logger.debug(
            "unread-nudge sender lookup failed for %r; count-only line",
            agent_id,
            exc_info=True,
        )
        return []


__all__ = ["maybe_append_unread_nudge"]
