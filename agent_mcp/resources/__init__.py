"""MCP resources subsystem (plan Phase 3).

This package exposes per-agent "ambient state" via the MCP resources
surface — clients (Claude Code, custom MCP consumers, etc.) can read
the calling agent's inbox and status counters via the spec-standard
`resources/list` + `resources/read` requests.

Two resources are exposed per caller, both scoped to the caller's
agent_id (derived server-side from the bearer):

* ``agent-mcp://inbox/<agent_id>`` — JSON envelope identical to
  what `wait_for_events` returns: ``{"events": [...],
  "next_cursor": "..."}``. Backed by the shared
  ``_collect_events_for(agent_id, since)`` helper from Phase 2.

* ``agent-mcp://status/<agent_id>`` — JSON counters:
  ``{"unread_messages": N, "unfinished_tasks": M, ...}``. Reflects
  the agent's current state at query time.

The two URIs co-exist because the consumer needs are different:
inbox is the event timeline (cleared by reading/processing); status
is the ambient counter snapshot (always current).

Notification emission (`notifications/resources/updated` on open
`GET /mcp` streams) is intentionally NOT implemented in this PR.
Stateless StreamableHTTP mode (the project's chosen transport per
PR #61) does not expose an enumeration API for in-flight GET
sessions, so cross-request fan-out requires a custom session
registry that's out of scope for this Phase. The resources are
fully polled-readable; long-poll wake-on-event continues to flow
through `wait_for_events`.
"""

from __future__ import annotations

from typing import Optional

from ..core.auth import get_agent_id

# Two URI prefixes scoped per-agent.
INBOX_URI_PREFIX = "agent-mcp://inbox/"
STATUS_URI_PREFIX = "agent-mcp://status/"


def resolve_agent_id_for_uri(uri: str, caller_token: Optional[str]) -> str:
    """Resolve which agent_id a `resources/read` URI is addressing,
    given the calling bearer.

    The URI carries the agent_id in its path — but the bearer always
    wins. If the URI's agent_id mismatches the bearer's agent_id, we
    raise ValueError so the caller cannot peek into another agent's
    inbox or status by guessing the URI. The framework converts the
    exception into a JSON-RPC error.
    """
    bearer_agent_id = get_agent_id(caller_token) if caller_token else None
    if not bearer_agent_id:
        raise ValueError("Unauthorized: token does not resolve to an agent")

    if uri.startswith(INBOX_URI_PREFIX):
        uri_agent_id = uri[len(INBOX_URI_PREFIX):].rstrip("/")
    elif uri.startswith(STATUS_URI_PREFIX):
        uri_agent_id = uri[len(STATUS_URI_PREFIX):].rstrip("/")
    else:
        raise ValueError(f"Unknown resource URI: {uri}")

    # Admin can read any agent's resource (operational visibility).
    # Other callers may only read their own.
    if bearer_agent_id == "admin":
        return uri_agent_id
    if uri_agent_id != bearer_agent_id:
        raise ValueError(
            "Unauthorized: callers may only read their own inbox / "
            "status resources"
        )
    return uri_agent_id
