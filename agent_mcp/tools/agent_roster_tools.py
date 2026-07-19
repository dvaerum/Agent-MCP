"""Peer roster tool `view_agents` (agent self-service profiles, plan §9).

Any authenticated agent (worker or manager) — and operator tiers — can
list every active agent's public identity + self-authored profile to
answer "who do I ask?". Feeds the existing `request_assistance` flow.

Deliberately narrow projection: `{agent_id, agent_role, profile,
profile_updated_at}`. No token, no working directory, no secrets — the
bearer/token column never leaves the repository. Tombstone / terminated
/ system rows are excluded (they are not agents you can talk to).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import register_tool
from ..core.config import logger
from ..core.principal import Principal
from ..core.tool_result import Ok, PermissionDenied, ToolResult
from ..utils.audit_utils import log_audit


# Statuses that are not a live, talk-to-able agent. Mirrors
# ``TERMINAL_AGENT_STATUSES`` (terminated/tombstone) plus the synthetic
# ``system`` pseudo-agent, which is not a peer.
_ROSTER_EXCLUDED_STATUSES = frozenset({"terminated", "tombstone", "system"})


def _principal_can_view_roster(principal: Optional[Principal]) -> bool:
    """True iff ``principal`` may list the roster.

    Admits any authenticated agent (workers/managers carry ``agents.use``)
    and operator tiers (operators/viewers carry ``agents.view``; sysadmin
    admits via the wildcard). Anonymous callers carry no caps and are
    rejected. Kept as an OR of the two agent/operator read caps because
    no single cap spans both the agent bundles and the operator bundles.
    """
    if principal is None:
        return False
    return principal.has_capability("agents.use") or principal.has_capability(
        "agents.view"
    )


async def view_agents_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """List every active agent's public identity + self-authored profile.

    Returns ``{"agents": [{agent_id, agent_role, profile,
    profile_updated_at}, ...]}`` sorted by ``agent_id``. Excludes
    terminated / tombstone / system rows. No token or secret fields.
    """
    if not _principal_can_view_roster(principal):
        return PermissionDenied(
            reason="An authenticated agent or operator is required to view "
            "the agent roster."
        )

    from ..repositories import agent_repo

    roster = []
    for row in agent_repo.list_active():
        if row.get("status") in _ROSTER_EXCLUDED_STATUSES:
            continue
        roster.append({
            "agent_id": row.get("agent_id"),
            "agent_role": row.get("agent_role") or "worker",
            "profile": row.get("profile"),
            "profile_updated_at": row.get("profile_updated_at"),
        })
    roster.sort(key=lambda r: r.get("agent_id") or "")

    if principal is not None:
        log_audit(principal.actor_label(), "view_agents", {"count": len(roster)})
    logger.info("view_agents: returned %d active agents.", len(roster))

    payload = {"agents": roster}
    return Ok(data=payload)


def register_agent_roster_tools() -> None:
    register_tool(
        name="view_agents",
        description=(
            "List every active agent on the team with their role and "
            "self-authored profile (what they do, what they work on, what "
            "to ask them about). Use this to find who to talk to or hand "
            "work to. Returns {\"agents\": [{agent_id, agent_role, "
            "profile, profile_updated_at}, ...]}."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_agents_tool_impl,
        visibility="worker",
    )


# Auto-register when imported.
register_agent_roster_tools()
