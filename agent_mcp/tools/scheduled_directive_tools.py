# Agent-MCP/agent_mcp/tools/scheduled_directive_tools.py
"""Agent-facing CRUD for scheduled directives (event-loop scheduler).

A **directive** is a recurring imperative an agent self-registers (or a
manager/operator registers for it) that fires *when the agent next checks
in* at-or-after the interval — wait-loop-native, no clock-blind
interrupt, no pile-up (plan §5).

Four tools, caller-scoped:

* ``create_scheduled_directive(prompt, interval_seconds, {agent_id?,
  until?, count?, run_now?})``
* ``list_scheduled_directives({agent_id?})``
* ``update_scheduled_directive(directive_id, {prompt?, interval_seconds?,
  enabled?, until?, count?})`` — edit / pause / resume
* ``delete_scheduled_directive(directive_id)``

Three-tier scope (plan §2 decision 5), enforced in-body, mirroring the
``config_allow_manager_curate_profiles`` precedent:

* **agent-self** — an agent manages its OWN schedules
  (``config_allow_worker_self_schedule``, default ON);
* **manager-for-workers** — a manager may manage any WORKER's schedules
  (``config_allow_manager_curate_schedules``, default ON); never another
  manager's;
* **operator/admin** — always (bypass), for any target.

Guardrails (decision 6), enforced at create/update:

* min-interval floor ``config_min_schedule_interval_seconds`` (default 60);
* max active loops per agent ``config_max_schedules_per_agent`` (default 10).
"""

from __future__ import annotations

import datetime
import secrets
from typing import Any, Dict, Optional

from .registry import register_tool
# R8-F1: explicit maxLength bound for identifier-shaped fields
# (agent_id, directive_id). See core/schema_limits.py for the
# rationale; `prompt` is free text and inherits DEFAULT_STRING_MAX_LEN
# from the dispatcher's generic backstop.
from ..core.schema_limits import IDENTIFIER_MAX_LEN
from . import access as _access
from ..core.authorize import requires_capability, requires_policy
from ..core.config import logger
from ..core import globals as g
from ..core.principal import Principal
from ..core.principal_builder import is_operator_tier
from ..core.tool_result import (
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..db.connection import get_db_connection
from ..db.unit_of_work import unit_of_work
from ..repositories import agent_repo
from ..repositories import scheduled_directive_repository as repo


# Agent statuses that make a row a non-target (mirrors the profile tool).
TERMINAL_AGENT_STATUSES = ("terminated", "tombstone")

# R16-F3/F4 upper bounds (sibling of PF-R18-1). ``int(<client value>)``
# must be bounded, not just floored: a huge finite interval passes the
# floor check then overflows ``now + timedelta(seconds=interval)``
# (timedelta caps near 2.7e6 days), and a huge finite count overflows
# SQLite's signed-64-bit INTEGER bind (2**63). Cap both at a sane value
# so the field-level ``Invalid`` fires before either overflow can.
MAX_INTERVAL_SECONDS = 315_360_000  # 10 years — well within timedelta's range
MAX_COUNT = 1_000_000  # a million fires — sane, and far under 2**63


def _generate_directive_id() -> str:
    return f"sd_{secrets.token_hex(8)}"


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _floor_seconds() -> int:
    return _access._get_config_int("config_min_schedule_interval_seconds")


def _max_per_agent() -> int:
    return _access._get_config_int("config_max_schedules_per_agent")


def _authorize_target_write(
    principal: Principal, target_agent_id: str
) -> Optional[ToolResult]:
    """Return a denial :class:`ToolResult`, or ``None`` when the caller
    may write schedules for ``target_agent_id``.

    Three-tier matrix (see module docstring). Operator-tier bypasses.
    """
    if is_operator_tier(principal):
        return None

    if principal.kind != "agent_bearer" or not principal.agent_id:
        return PermissionDenied(
            reason="Valid agent token or operator session required"
        )

    caller_id = principal.agent_id
    caller_role = principal.agent_role or "worker"
    is_self = target_agent_id == caller_id

    if is_self:
        if not _access._get_config_bool("config_allow_worker_self_schedule"):
            return PermissionDenied(
                reason=(
                    "Self-scheduling is disabled by the operator "
                    "(config_allow_worker_self_schedule). Ask a manager or "
                    "admin to create the schedule for you."
                )
            )
        return None

    # Targeting another agent → manager curation only.
    if caller_role != "manager":
        return PermissionDenied(
            reason=(
                "Only a manager may schedule directives for another agent. "
                "Workers may only manage their own schedules."
            )
        )
    if not _access._get_config_bool("config_allow_manager_curate_schedules"):
        return PermissionDenied(
            reason=(
                "Manager schedule-curation is disabled by the operator "
                "(config_allow_manager_curate_schedules)."
            )
        )
    target = agent_repo.get_by_id(target_agent_id)
    if target is None or target.get("status") in TERMINAL_AGENT_STATUSES:
        return NotFound(resource="agent", identifier=target_agent_id)
    # Managers curate WORKERS only — never another manager.
    if (target.get("agent_role") or "worker") != "worker":
        return PermissionDenied(
            reason="Managers may only schedule directives for workers."
        )
    return None


def _authorize_existing_or_notfound(
    principal: Principal, existing: Dict[str, Any], directive_id: str
) -> Optional[ToolResult]:
    """Authorize a write against an EXISTING directive without leaking that
    it exists. Returns a denial :class:`ToolResult`, or ``None`` to proceed.

    R17-F2: update/delete looked the row up first and then returned the raw
    authorization denial, so a non-owner worker could distinguish "exists
    but forbidden" (``PermissionDenied``) from "missing" (``NotFound``) — an
    existence oracle. Collapse the two: any caller who is neither the owner
    nor operator-tier gets the SAME opaque ``NotFound`` a missing id yields
    (mirrors the tasks/notes phantom-not-found pattern, and the pre-lookup
    authz already used by ``list_scheduled_directives``). The owning worker
    still gets the real reason (e.g. self-scheduling toggled off); a manager
    curating its worker (or an operator) still gets real access.
    """
    auth_denial = _authorize_target_write(principal, existing["agent_id"])
    if auth_denial is None:
        return None
    caller_id = principal.agent_id if principal.kind == "agent_bearer" else None
    if existing["agent_id"] != caller_id:
        return NotFound(
            resource="scheduled directive", identifier=directive_id
        )
    return auth_denial


def _validate_interval(raw: Any) -> tuple[Optional[int], Optional[ToolResult]]:
    """Coerce + floor/ceiling-check an interval (seconds)."""
    try:
        interval = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError (R16-F3): a JSON number token like ``1e400``
        # parses to ``float('inf')`` via ``json.loads``, and
        # ``int(float('inf'))`` raises OverflowError — a sibling the
        # PF-R18-1 fix caught for messages/grace_days but scheduling
        # never got. Reject it as a clean field error, not a raw 500.
        return None, Invalid(
            field="interval_seconds",
            message="interval_seconds must be an integer number of seconds",
        )
    floor = _floor_seconds()
    if interval < floor:
        return None, Invalid(
            field="interval_seconds",
            message=(
                f"interval_seconds must be at least the configured floor "
                f"of {floor}s (config_min_schedule_interval_seconds). "
                f"Got {interval}."
            ),
        )
    # Upper bound (R16-F3): a huge finite interval clears the floor then
    # overflows ``now + timedelta(seconds=interval)`` downstream.
    if interval > MAX_INTERVAL_SECONDS:
        return None, Invalid(
            field="interval_seconds",
            message=(
                f"interval_seconds must be at most {MAX_INTERVAL_SECONDS}s "
                f"(10 years). Got {interval}."
            ),
        )
    return interval, None


def _validate_until(raw: Any) -> tuple[Optional[str], Optional[ToolResult]]:
    """Validate an optional ``until`` end-condition (ISO datetime string).

    Returns ``(until_iso_or_None, denial_or_None)``. ``None`` input →
    ``(None, None)`` (no end-condition).
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, Invalid(
            field="until",
            message="until must be an ISO-8601 datetime string",
        )
    try:
        until_dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None, Invalid(
            field="until",
            message="until must be a valid ISO-8601 datetime string",
        )
    # R17-F1: a tz-aware ``until`` (offset or trailing 'Z') must not blow up
    # the comparison below with ``TypeError: can't compare offset-naive and
    # offset-aware datetimes`` (which escaped this ValueError-only guard and
    # surfaced as a raw 500 / generic Failed — a direct sibling of the
    # R16-F3/F4 interval/count hardening in this same file). Normalize a
    # tz-aware value to the module's naive-LOCAL convention before both the
    # future-check AND storage: everything downstream compares ``until_at``
    # LEXICALLY against naive-local ISO strings (``next_due`` in create,
    # ``now_iso`` in the repository's collect_due_and_fire / soonest_due), so
    # a stored tz-aware string would silently mis-order those windows. This
    # mirrors the tz-safe pattern in app/routers/agents.py:_within_grace.
    if until_dt.tzinfo is not None:
        until_dt = until_dt.astimezone().replace(tzinfo=None)
    if until_dt <= _now():
        return None, Invalid(
            field="until",
            message="until must be in the future",
        )
    return until_dt.isoformat(), None


def _validate_count(raw: Any) -> tuple[Optional[int], Optional[ToolResult]]:
    if raw is None:
        return None, None
    try:
        count = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError (R16-F4): ``int(float('inf'))`` from a JSON
        # ``1e400`` token — same sibling as interval_seconds above.
        return None, Invalid(
            field="count", message="count must be a positive integer"
        )
    if count < 1:
        return None, Invalid(
            field="count", message="count must be a positive integer"
        )
    # Upper bound (R16-F4): a huge finite count overflows SQLite's
    # signed-64-bit INTEGER bind (``max_runs`` column) → a generic 500.
    if count > MAX_COUNT:
        return None, Invalid(
            field="count",
            message=f"count must be at most {MAX_COUNT}. Got {count}.",
        )
    return count, None


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    """Public-facing directive shape for tool responses."""
    return {
        "directive_id": row["directive_id"],
        "agent_id": row["agent_id"],
        "prompt": row["prompt"],
        "interval_seconds": row["interval_seconds"],
        "next_due_at": row["next_due_at"],
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "until_at": row["until_at"],
        "max_runs": row["max_runs"],
        "run_count": row["run_count"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


@requires_policy(
    "config_allow_worker_self_schedule",
    "config_allow_manager_curate_schedules",
)
async def create_scheduled_directive_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Principal,
) -> ToolResult:
    """Register a recurring directive.

    First fire is one interval out (``next_due = created_at + interval``);
    pass ``run_now: true`` for an immediate first fire (``next_due = now``)
    — decision 8.
    """
    prompt = arguments.get("prompt")
    if not prompt or not isinstance(prompt, str):
        return Invalid(field="prompt", message="prompt is required")
    if len(prompt) > 4000:
        return Invalid(
            field="prompt", message="prompt too long (max 4000 characters)"
        )

    interval, denial = _validate_interval(arguments.get("interval_seconds"))
    if denial is not None:
        return denial
    until_iso, denial = _validate_until(arguments.get("until"))
    if denial is not None:
        return denial
    max_runs, denial = _validate_count(arguments.get("count"))
    if denial is not None:
        return denial

    caller_id = principal.agent_id if principal.kind == "agent_bearer" else None
    target_raw = arguments.get("agent_id")
    if target_raw is not None and not isinstance(target_raw, str):
        return Invalid(field="agent_id", message="agent_id must be a string")
    target_agent_id = (target_raw or caller_id)
    if not target_agent_id:
        return Invalid(
            field="agent_id",
            message="agent_id is required (no calling agent to default to)",
        )

    auth_denial = _authorize_target_write(principal, target_agent_id)
    if auth_denial is not None:
        return auth_denial

    run_now = bool(arguments.get("run_now", False))
    now = _now()
    if run_now:
        next_due = now
    else:
        next_due = now + datetime.timedelta(seconds=interval)
    # A run_now/first fire must not already be past its window.
    if until_iso is not None and next_due.isoformat() > until_iso and not run_now:
        return Invalid(
            field="until",
            message="until is before the first fire — nothing would run",
        )

    directive_id = _generate_directive_id()
    try:
        with unit_of_work() as u:
            # Guardrail: max active loops per agent (decision 6).
            active = repo.count_active_for_agent(
                target_agent_id, connection=u.cursor
            )
            cap = _max_per_agent()
            if active >= cap:
                return Invalid(
                    field="interval_seconds",
                    message=(
                        f"agent '{target_agent_id}' already has {active} "
                        f"active schedules (max {cap}, "
                        f"config_max_schedules_per_agent). Delete or pause "
                        "one first."
                    ),
                )
            created = repo.create(
                directive_id=directive_id,
                agent_id=target_agent_id,
                prompt=prompt,
                interval_seconds=interval,
                next_due_at=next_due.isoformat(),
                until_at=until_iso,
                max_runs=max_runs,
                created_by=principal.actor_label(),
                connection=u.cursor,
                now_iso=now.isoformat(),
            )
            u.audit(
                principal.actor_label(),
                "create_scheduled_directive",
                details={
                    "directive_id": directive_id,
                    "agent_id": target_agent_id,
                    "interval_seconds": interval,
                    "run_now": run_now,
                },
            )
            # Wake a currently-holding wait_for_events so a run_now (or an
            # immediately-due) schedule fires now rather than on the next
            # ~2s flag-recheck slice.
            u.on_commit(
                lambda a=target_agent_id: g.notify_agent_inbox(a)
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.error("create_scheduled_directive failed: %s", e, exc_info=True)
        return Failed(message="Failed to create scheduled directive")

    return Ok(
        data={"directive": _serialize(created)},
        message=(
            f"Scheduled directive {directive_id} created for "
            f"{target_agent_id} (every {interval}s, first fire "
            f"{'now' if run_now else 'in ' + str(interval) + 's'})."
        ),
    )


@requires_capability("coordination.wait")
async def list_scheduled_directives_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Principal,
) -> ToolResult:
    """List the caller's scheduled directives (or a target's, scoped)."""
    caller_id = principal.agent_id if principal.kind == "agent_bearer" else None
    target_raw = arguments.get("agent_id")
    if target_raw is not None and not isinstance(target_raw, str):
        return Invalid(field="agent_id", message="agent_id must be a string")
    target_agent_id = target_raw or caller_id
    if not target_agent_id:
        return Invalid(
            field="agent_id",
            message="agent_id is required (no calling agent to default to)",
        )
    # Reading another agent's schedules is gated the same way as writing.
    if target_agent_id != caller_id:
        auth_denial = _authorize_target_write(principal, target_agent_id)
        if auth_denial is not None:
            return auth_denial

    conn = None
    try:
        conn = get_db_connection()
        rows = repo.list_for_agent(target_agent_id, connection=conn.cursor())
    except Exception as e:  # pragma: no cover - defensive
        logger.error("list_scheduled_directives failed: %s", e, exc_info=True)
        return Failed(message="Failed to list scheduled directives")
    finally:
        if conn:
            conn.close()

    directives = [_serialize(r) for r in rows]
    return Ok(
        data={"agent_id": target_agent_id, "directives": directives,
              "count": len(directives)},
        message=(
            f"{len(directives)} scheduled directive(s) for {target_agent_id}."
        ),
    )


@requires_policy(
    "config_allow_worker_self_schedule",
    "config_allow_manager_curate_schedules",
)
async def update_scheduled_directive_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Principal,
) -> ToolResult:
    """Edit / pause / resume a scheduled directive.

    ``enabled: false`` pauses (status→paused); ``enabled: true`` resumes
    (status→active) and re-arms ``next_due`` one interval out. Changing
    the interval re-validates the floor; re-enabling re-checks the
    per-agent cap.
    """
    directive_id = arguments.get("directive_id")
    if not directive_id or not isinstance(directive_id, str):
        return Invalid(
            field="directive_id", message="directive_id is required"
        )

    now = _now()
    try:
        with unit_of_work() as u:
            existing = repo.get(directive_id, connection=u.cursor)
            if existing is None:
                return NotFound(
                    resource="scheduled directive", identifier=directive_id
                )
            auth_denial = _authorize_existing_or_notfound(
                principal, existing, directive_id
            )
            if auth_denial is not None:
                return auth_denial

            fields: Dict[str, Any] = {}

            if "prompt" in arguments and arguments["prompt"] is not None:
                prompt = arguments["prompt"]
                if not isinstance(prompt, str) or not prompt:
                    return Invalid(
                        field="prompt", message="prompt must be a non-empty string"
                    )
                if len(prompt) > 4000:
                    return Invalid(
                        field="prompt",
                        message="prompt too long (max 4000 characters)",
                    )
                fields["prompt"] = prompt

            new_interval = existing["interval_seconds"]
            if (
                "interval_seconds" in arguments
                and arguments["interval_seconds"] is not None
            ):
                new_interval, denial = _validate_interval(
                    arguments["interval_seconds"]
                )
                if denial is not None:
                    return denial
                fields["interval_seconds"] = new_interval

            if "until" in arguments:
                until_iso, denial = _validate_until(arguments["until"])
                if denial is not None:
                    return denial
                fields["until_at"] = until_iso

            if "count" in arguments:
                max_runs, denial = _validate_count(arguments["count"])
                if denial is not None:
                    return denial
                fields["max_runs"] = max_runs

            if "enabled" in arguments and arguments["enabled"] is not None:
                enabled = bool(arguments["enabled"])
                if enabled:
                    # Resume: re-check the per-agent cap (excluding self if
                    # already active) and re-arm next_due one interval out.
                    if not existing["enabled"]:
                        active = repo.count_active_for_agent(
                            existing["agent_id"], connection=u.cursor
                        )
                        cap = _max_per_agent()
                        if active >= cap:
                            return Invalid(
                                field="enabled",
                                message=(
                                    f"agent '{existing['agent_id']}' already "
                                    f"has {active} active schedules (max "
                                    f"{cap}). Pause or delete one first."
                                ),
                            )
                    fields["enabled"] = 1
                    fields["status"] = "active"
                    fields["next_due_at"] = (
                        now + datetime.timedelta(seconds=new_interval)
                    ).isoformat()
                else:
                    fields["enabled"] = 0
                    fields["status"] = "paused"

            if not fields:
                return Invalid(
                    field=None,
                    message=(
                        "no updatable field provided (prompt, "
                        "interval_seconds, enabled, until, count)"
                    ),
                )

            updated = repo.update_fields(
                directive_id,
                fields,
                updated_by=principal.actor_label(),
                connection=u.cursor,
            )
            u.audit(
                principal.actor_label(),
                "update_scheduled_directive",
                details={
                    "directive_id": directive_id,
                    "agent_id": existing["agent_id"],
                    "fields": sorted(fields.keys()),
                },
            )
            u.on_commit(
                lambda a=existing["agent_id"]: g.notify_agent_inbox(a)
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.error("update_scheduled_directive failed: %s", e, exc_info=True)
        return Failed(message="Failed to update scheduled directive")

    return Ok(
        data={"directive": _serialize(updated)},
        message=f"Scheduled directive {directive_id} updated.",
    )


@requires_policy(
    "config_allow_worker_self_schedule",
    "config_allow_manager_curate_schedules",
)
async def delete_scheduled_directive_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Principal,
) -> ToolResult:
    """Delete a scheduled directive permanently."""
    directive_id = arguments.get("directive_id")
    if not directive_id or not isinstance(directive_id, str):
        return Invalid(
            field="directive_id", message="directive_id is required"
        )
    try:
        with unit_of_work() as u:
            existing = repo.get(directive_id, connection=u.cursor)
            if existing is None:
                return NotFound(
                    resource="scheduled directive", identifier=directive_id
                )
            auth_denial = _authorize_existing_or_notfound(
                principal, existing, directive_id
            )
            if auth_denial is not None:
                return auth_denial
            repo.delete(directive_id, connection=u.cursor)
            u.audit(
                principal.actor_label(),
                "delete_scheduled_directive",
                details={
                    "directive_id": directive_id,
                    "agent_id": existing["agent_id"],
                },
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.error("delete_scheduled_directive failed: %s", e, exc_info=True)
        return Failed(message="Failed to delete scheduled directive")

    return Ok(
        data={"deleted": directive_id},
        message=f"Scheduled directive {directive_id} deleted.",
    )


def register_scheduled_directive_tools() -> None:
    """Register the four scheduled-directive CRUD tools."""
    _sched_props = {
        "prompt": {
            "type": "string",
            "description": (
                "The imperative directive text delivered to the agent when "
                "the schedule fires (e.g. 'check the CI status and report')."
            ),
        },
        "interval_seconds": {
            "type": "integer",
            "description": (
                "How often the directive fires, in seconds. Must be >= the "
                "operator's floor (config_min_schedule_interval_seconds, "
                "default 60). The interval resets from each delivery, so a "
                "busy agent never piles up fires."
            ),
            "minimum": 1,
        },
        "agent_id": {
            "type": "string",
            "description": (
                "Target agent. Omit to schedule for yourself. A manager may "
                "target one of its workers; an operator may target anyone."
            ),
            "maxLength": IDENTIFIER_MAX_LEN,
        },
        "until": {
            "type": ["string", "null"],
            "description": (
                "Optional end-condition: an ISO-8601 datetime after which "
                "the schedule stops firing and becomes 'completed'."
            ),
        },
        "count": {
            "type": ["integer", "null"],
            "description": (
                "Optional end-condition: stop after this many fires "
                "(then 'completed')."
            ),
            "minimum": 1,
        },
        "run_now": {
            "type": "boolean",
            "description": (
                "When true, the first fire is immediate (next check-in) "
                "instead of one interval out. Default false."
            ),
            "default": False,
        },
    }

    register_tool(
        name="create_scheduled_directive",
        description=(
            "Register a recurring directive that fires when the target "
            "agent next checks in at-or-after the interval (a durable, "
            "server-side, event-coalesced '/loop'). By default an agent "
            "schedules for itself; a manager may schedule for its workers. "
            "An enabled schedule keeps the agent alive past the idle-stop "
            "window so it can receive fires."
        ),
        input_schema={
            "type": "object",
            "properties": _sched_props,
            "required": ["prompt", "interval_seconds"],
            "additionalProperties": False,
        },
        implementation=create_scheduled_directive_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_self_schedule,"
            "config_allow_manager_curate_schedules"
        ),
    )

    register_tool(
        name="list_scheduled_directives",
        description=(
            "List scheduled directives for the calling agent (or, for a "
            "manager/operator, a named target). Returns id, prompt, "
            "interval, next_due_at, enabled, status, and run_count."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Target agent whose schedules to list. Omit for "
                        "your own."
                    ),
                    "maxLength": IDENTIFIER_MAX_LEN,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=list_scheduled_directives_tool_impl,
    )

    register_tool(
        name="update_scheduled_directive",
        description=(
            "Edit, pause, or resume a scheduled directive. Set "
            "enabled=false to pause and enabled=true to resume (re-arms "
            "the next fire one interval out). Interval changes re-validate "
            "the floor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "directive_id": {
                    "type": "string",
                    "description": "The id of the schedule to update.",
                    "maxLength": IDENTIFIER_MAX_LEN,
                },
                "prompt": {
                    "type": ["string", "null"],
                    "description": "New directive text.",
                },
                "interval_seconds": {
                    "type": ["integer", "null"],
                    "description": "New interval (seconds); re-checks floor.",
                    "minimum": 1,
                },
                "enabled": {
                    "type": ["boolean", "null"],
                    "description": "true=resume, false=pause.",
                },
                "until": {
                    "type": ["string", "null"],
                    "description": (
                        "New until end-condition (ISO datetime), or null to "
                        "clear it."
                    ),
                },
                "count": {
                    "type": ["integer", "null"],
                    "description": (
                        "New max-runs end-condition, or null to clear it."
                    ),
                    "minimum": 1,
                },
            },
            "required": ["directive_id"],
            "additionalProperties": False,
        },
        implementation=update_scheduled_directive_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_self_schedule,"
            "config_allow_manager_curate_schedules"
        ),
    )

    register_tool(
        name="delete_scheduled_directive",
        description="Delete a scheduled directive permanently.",
        input_schema={
            "type": "object",
            "properties": {
                "directive_id": {
                    "type": "string",
                    "description": "The id of the schedule to delete.",
                    "maxLength": IDENTIFIER_MAX_LEN,
                },
            },
            "required": ["directive_id"],
            "additionalProperties": False,
        },
        implementation=delete_scheduled_directive_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_self_schedule,"
            "config_allow_manager_curate_schedules"
        ),
    )


register_scheduled_directive_tools()
