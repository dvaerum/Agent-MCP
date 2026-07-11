"""Inbox resource — JSON event envelope for the calling agent.

Routes through `assemble_event_feed` — the single owner of the
event-feed stream-merge pipeline — so the read response is truly
byte-identical to what `wait_for_events` returns for the same `since`
cursor. (The previous `_collect_events_for` shim OMITTED the
unassigned-task stream + the merged-boundary clamp, so the inbox
silently diverged from `wait_for_events` despite this docstring's
claim — routing both through the one owner makes that divergence
unrepresentable.)
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
    from ..tools.agent_communication_tools import assemble_event_feed

    events, next_cursor = assemble_event_feed(agent_id, since)
    return json.dumps(
        {"events": events, "next_cursor": next_cursor},
        ensure_ascii=False,
    )
