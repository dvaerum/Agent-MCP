"""In-memory record of each agent's MCP ``clientInfo`` handshake.

Why this exists: the ``/mcp`` transport runs in STATELESS Streamable-HTTP
mode, so the SDK creates a fresh session per POST and
``session.client_params`` is ``None`` at tool-call time — the client's
``initialize`` handshake (which carries ``clientInfo.name``) arrives on a
SEPARATE earlier POST. The client sends ``initialize`` once when it
(re)connects, then many tool calls. So we capture ``clientInfo`` at the
initialize POST (``main_app._maybe_record_client_info``, which reuses the
already-drained request body) and stash it here keyed by agent_id; the
``wait_for_events`` hold-strategy resolver reads it back on each call.

In-memory is sufficient: on a backend restart every client's connection
drops and re-initializes, repopulating this map before any tool call on
the fresh connection. Absent an entry (races, a client that sent no
clientInfo), the resolver falls back to feature-detect on the
progressToken — the safe default.
"""

from __future__ import annotations

from typing import Optional

from .config import logger

#: agent_id -> {"name": <str>, "version": <str>}. Bounded by the number of
#: distinct agents that have connected this process lifetime.
_CLIENT_INFO: dict[str, dict[str, str]] = {}


def record_client_info(
    agent_id: str, name: str, version: Optional[str] = None
) -> None:
    """Record the ``clientInfo`` an agent sent at its MCP ``initialize``.

    Idempotent-overwrite: a reconnect re-records the (possibly updated)
    identity. Logs the exact name+version so operators can confirm what a
    real client advertises (and graduate an unknown client into the
    identity table).
    """
    if not agent_id or not name:
        return
    _CLIENT_INFO[agent_id] = {"name": name, "version": version or ""}
    # Emit at WARNING when event-loop debug is on so journald's WARNING-level
    # stderr handler actually captures it (the plain INFO below is invisible
    # in the systemd journal) — this is how an operator confirms what a real
    # client advertises and graduates an unknown name into the hold-strategy
    # table. Toggled by the ``config_debug_eventloop`` project setting (falls
    # back to the AGENT_MCP_EVENTLOOP_DEBUG env var).
    from .debug_flags import debug_enabled

    if debug_enabled("config_debug_eventloop", "AGENT_MCP_EVENTLOOP_DEBUG"):
        logger.warning(
            "EVENTLOOP client identity recorded: agent=%s clientInfo.name=%r "
            "version=%r",
            agent_id, name, version,
        )
    logger.info(
        "MCP client identity recorded: agent=%s clientInfo.name=%r version=%r",
        agent_id,
        name,
        version,
    )


def get_client_name(agent_id: str) -> Optional[str]:
    """The raw ``clientInfo.name`` last recorded for ``agent_id``, or None.

    Returned verbatim (un-normalized) — the strategy resolver normalizes
    at lookup time so the stored value stays faithful to the wire.
    """
    if not agent_id:
        return None
    info = _CLIENT_INFO.get(agent_id)
    return info.get("name") if info else None


def clear() -> None:
    """Drop all recorded identities (test isolation helper)."""
    _CLIENT_INFO.clear()
