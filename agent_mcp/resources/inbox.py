"""Inbox resource — JSON event envelope for the calling agent.

Reuses `_collect_events_for` from the agent_communication_tools
module so the read response is byte-identical to what
`wait_for_events` returns for the same `since` cursor.
"""

from __future__ import annotations

import json
from typing import Optional


def render_inbox(agent_id: str, since: Optional[str] = None) -> str:
    """Return the inbox JSON envelope for `agent_id`.

    `since` is optional — when absent the helper returns the full
    history. In practice MCP `resources/read` calls don't carry a
    cursor (the read API has no query params per spec); callers
    poll the resource and dedupe client-side via `message_id` /
    `task_id`. For cursor-based consumption use `wait_for_events`.
    """
    from ..tools.agent_communication_tools import (
        _collect_events_for,
    )

    events = _collect_events_for(agent_id, since)
    if events:
        next_cursor = max(e["timestamp"] for e in events)
    else:
        next_cursor = since or ""
    return json.dumps(
        {"events": events, "next_cursor": next_cursor},
        ensure_ascii=False,
    )
