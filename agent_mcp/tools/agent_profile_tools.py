"""Agent self-service profile tool (agent self-service profiles, plan §2).

One agent-facing tool, ``update_agent_profile``, lets an agent keep its
own free-text ``profile`` current — and lets a manager curate its team's
worker profiles. The server does the review-vs-change bookkeeping
(``AgentRepository.review_profile``): every call bumps
``profile_reviewed_at`` (drives the staleness nudge); only a real content
change bumps ``profile_updated_at`` + ``profile_updated_by`` and reaches
the peer broadcast.

The tool is routing-neutral — editing a profile has no operational side
effect — so the operator's on/off toggles here are a GOVERNANCE
preference, not a safety gate:

* ``config_allow_worker_update_own_profile``  (default True) — a worker
  editing/confirming its OWN profile.
* ``config_allow_manager_update_own_profile`` (default True) — a manager
  editing/confirming its OWN profile.
* ``config_allow_manager_curate_profiles``    (default True) — a manager
  editing a WORKER's profile. Managers may never edit another manager's
  profile regardless of this toggle.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import register_tool
from ..core.authorize import requires_capability
from ..core.config import logger
from ..core.principal import Principal
from ..core.tool_result import (
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..repositories.agent_repository import TERMINAL_AGENT_STATUSES
from ..utils.audit_utils import log_audit
from . import access as _access


@requires_capability("agents.use")
async def update_agent_profile_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Update or confirm an agent's self-authored profile.

    Arguments:
        profile: The new profile prose. OMIT this argument to
            "confirm still accurate" — that bumps ``profile_reviewed_at``
            only (no peer broadcast). Providing identical content is also
            a no-op change (reviewed_at moves, updated_at does not).
        agent_id: Target agent. Optional; defaults to the caller. Only a
            manager may target ANOTHER agent, and only a worker in the
            same project (manager curation). Managers may not edit other
            managers.

    Gating (all default-on governance toggles, plan §5):
        * self-edit worker   → ``config_allow_worker_update_own_profile``
        * self-edit manager  → ``config_allow_manager_update_own_profile``
        * edit-another        → ``config_allow_manager_curate_profiles``
          AND caller is a manager AND target is a worker in the project.

    Returns ``{profile, changed, reviewed_at}``.
    """
    if (
        principal is None
        or principal.kind != "agent_bearer"
        or not principal.agent_id
    ):
        return PermissionDenied(
            reason="A valid agent token is required to update a profile."
        )
    caller_id = principal.agent_id
    caller_role = principal.agent_role or "worker"

    # ``profile`` omitted ⇒ review-confirm (bumps reviewed_at only). When
    # present it must be a string (empty string is allowed — it clears
    # the profile back to "unset").
    profile_arg = arguments.get("profile")
    if profile_arg is not None and not isinstance(profile_arg, str):
        return Invalid(
            field="profile",
            message="profile must be a string when provided.",
        )

    target_arg = arguments.get("agent_id")
    if target_arg is not None and not isinstance(target_arg, str):
        return Invalid(
            field="agent_id",
            message="agent_id must be a string when provided.",
        )
    target_id = target_arg.strip() if isinstance(target_arg, str) else ""
    if not target_id:
        target_id = caller_id
    is_self = target_id == caller_id

    from ..repositories import agent_repo

    if is_self:
        # Self-edit / self-confirm governance toggle.
        if caller_role == "manager":
            if not _access._get_config_bool(
                "config_allow_manager_update_own_profile"
            ):
                return PermissionDenied(
                    reason=(
                        "Manager self-profile updates are disabled by the "
                        "operator (config_allow_manager_update_own_profile)."
                    )
                )
        else:
            if not _access._get_config_bool(
                "config_allow_worker_update_own_profile"
            ):
                return PermissionDenied(
                    reason=(
                        "Worker self-profile updates are disabled by the "
                        "operator (config_allow_worker_update_own_profile)."
                    )
                )
    else:
        # Manager curation of another agent's profile.
        if caller_role != "manager":
            return PermissionDenied(
                reason="Only a manager may edit another agent's profile."
            )
        if not _access._get_config_bool(
            "config_allow_manager_curate_profiles"
        ):
            return PermissionDenied(
                reason=(
                    "Manager profile curation is disabled by the operator "
                    "(config_allow_manager_curate_profiles)."
                )
            )
        target = agent_repo.get_by_id(target_id)
        if (
            target is None
            or target.get("status") in TERMINAL_AGENT_STATUSES
        ):
            return NotFound(resource="agent", identifier=target_id)
        # Managers curate WORKERS only. Peer-manager curation is denied
        # (plan §5 + §11 — an additive gate later if ever wanted). Agents
        # in one backend DB are all in the same project by construction,
        # so no cross-project check is needed here.
        if (target.get("agent_role") or "worker") != "worker":
            return PermissionDenied(
                reason=(
                    "Managers may only edit workers' profiles, not other "
                    "managers'."
                )
            )

    result = agent_repo.review_profile(
        target_id,
        new_profile=profile_arg,
        editor_id=caller_id,
    )
    if result is None:
        return NotFound(resource="agent", identifier=target_id)

    changed = bool(result.get("changed"))
    if changed:
        # Notify peers of a real content change (in-memory push; catch-up
        # via the agents table is the source of truth). PR2 ships the
        # notifier; guard the import so PR1 lands independently.
        try:
            from .agent_communication_tools import (
                notify_agent_profile_updated,
            )

            notify_agent_profile_updated(
                subject_id=target_id, editor_id=caller_id,
            )
        except (ImportError, AttributeError):
            # PR2 not yet merged — catch-up still delivers the event.
            pass

    log_audit(
        caller_id,
        "update_agent_profile",
        {"target": target_id, "changed": changed},
    )
    logger.info(
        "Agent %r %s profile of %r (changed=%s).",
        caller_id,
        "reviewed" if not changed else "updated",
        target_id,
        changed,
    )

    payload = {
        "agent_id": target_id,
        "profile": result.get("profile"),
        "changed": changed,
        "reviewed_at": result.get("profile_reviewed_at"),
        "updated_at": result.get("profile_updated_at"),
    }
    return Ok(
        data=payload,
        message=(
            "Profile updated."
            if changed
            else "Profile review recorded (no content change)."
        ),
    )


def register_agent_profile_tools() -> None:
    register_tool(
        name="update_agent_profile",
        description=(
            "Update or confirm your own self-authored profile — a short "
            "free-text description of what you do, what you work on, what "
            "tools/equipment you have, how you work, and what peers should "
            "ask you about. OMIT the `profile` argument to confirm your "
            "current profile is still accurate (records a review without "
            "changing anything). Managers may also pass `agent_id` to "
            "curate a worker's profile. Use `view_agents` to see the "
            "team's profiles."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": (
                        "The new profile prose. Omit to confirm your "
                        "existing profile is still accurate (review-only)."
                    ),
                },
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Target agent id. Optional; defaults to you. Only "
                        "a manager may target another agent, and only a "
                        "worker in the project."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=update_agent_profile_tool_impl,
    )


# Auto-register when imported.
register_agent_profile_tools()
