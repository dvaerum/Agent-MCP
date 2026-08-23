# Agent-MCP/agent_mcp/tools/project_settings_tools.py
"""MCP tool family for the ``project_settings`` store (ADR-0016).

Wave 11 PR 0: per-project operational config (``config_*`` keys) lives
in the dedicated ``project_settings`` table — **settings** — while
``project_context`` holds only agent-authored shared knowledge —
**memory**. This module is the single write/read tool surface for the
settings store; the REST router (``app/routers/settings.py``)
dispatches these tools so there is ONE enforcement path.

Deliberately SMALL and deep — settings has none of the context tools'
machinery (no creator-ownership matrix, no backups, no bulk paths, no
RAG coupling). Three tools:

* ``view_project_settings`` — operator read; any genuinely secret keys
  (:data:`_SECRET_SETTING_KEYS`) mask for non-confirmed tiers.
* ``update_project_settings`` — upsert; ``system.config.write`` cap
  required.
* ``delete_project_settings`` — same gate as update; fires the same
  post-write wakes (a deleted toggle reverts to its default, so worker
  tool visibility / in-flight waiters may need to re-evaluate).

Secret classification is derived from the schema registry (every spec
with ``type == "secret"``) — the settings store knows its own schema,
so no prefix heuristic (the prefix heuristic on the mixed store is
exactly what caused F009).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

from .registry import register_tool
# R8-F1: explicit maxLength bound for the identifier-shaped
# context_key field. See core/schema_limits.py for the rationale.
from ..core.schema_limits import IDENTIFIER_MAX_LEN
from .project_context_tools import emit_context_write_wakes
from ..core.config import logger
from ..core.operator_tier import (
    is_confirmed_operator_tier as _shared_is_confirmed_operator_tier,
)
from ..core.authorize import requires_capability
from ..core.principal import Principal
from ..core.settings_schema import SECRET_SETTING_KEYS
from ..core.tool_result import (
    Failed,
    Invalid,
    NotFound,
    Ok,
    ToolResult,
)
from ..db.actions.agent_actions_db import log_agent_action_to_db
from ..db.unit_of_work import unit_of_work
from ..repositories import project_settings_repository as settings_repo
from ..utils.audit_utils import log_audit


# The settings store's OWN secret classification, derived from the
# single-source schema registry (``core/settings_schema.SECRET_SETTING_
# KEYS`` = every spec with ``type == "secret"``) so the secret
# classification and the schema's type column can never drift. A secret
# key masks to ``[redacted]`` for non-confirmed tiers; every other
# ``config_*`` row is an operator-readable toggle/knob. (ADR-0016 chose
# a schema-derived set over a prefix heuristic — the heuristic on the
# mixed store is exactly what caused F009.)
_SECRET_SETTING_KEYS = SECRET_SETTING_KEYS

_REDACTED_VALUE = "[redacted]"

# The settings store holds the ``config_*`` namespace ONLY — knowledge
# belongs in project_context.
_CONFIG_KEY_RE = re.compile(r"^config_", re.IGNORECASE)


def _actor_label(principal: Optional[Principal]) -> str:
    if principal is None:
        return "unknown"
    return principal.actor_label() or "unknown"


def _is_confirmed_operator_tier(principal: Optional[Principal]) -> bool:
    """MCP-side adapter over the shared confirmed-tier predicate
    (``core/operator_tier``) — same shape as ``tools/admin_tools``'s.
    Per-agent operator-tier bearers (manager/admin) are confirmed;
    cookie/forwarding callers only when the seam proves the role."""
    if principal is None:
        return False
    return _shared_is_confirmed_operator_tier(
        kind=principal.kind,
        sysadmin=principal.sysadmin,
        project_role=principal.project_role,
        agent_role=principal.agent_role,
    )


def redact_settings_row(
    row: Dict[str, Any], *, confirmed_operator_tier: bool,
) -> Dict[str, Any]:
    """Mask a :data:`_SECRET_SETTING_KEYS` value for non-confirmed
    tiers. Shared by the MCP view tool and the REST
    ``GET /api/settings-data`` seam so the two cannot drift. Every
    other row passes through with its REAL value — blanket redaction
    of the store is exactly the F009 bug the split removed."""
    if confirmed_operator_tier:
        return row
    if row.get("context_key") in _SECRET_SETTING_KEYS:
        return {**row, "value": _REDACTED_VALUE}
    return row


# --- view_project_settings ------------------------------------------------


@requires_capability("system.config.write")
async def view_project_settings_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Return every ``project_settings`` row (operator-only read)."""
    requesting_actor = _actor_label(principal)
    log_audit(requesting_actor, "view_project_settings", {})

    try:
        with unit_of_work() as u:
            rows = settings_repo.list_all(connection=u.cursor)
    except sqlite3.Error as e:
        logger.error(f"Database error reading project settings: {e}",
                     exc_info=True)
        return Failed(message="Database error reading project settings")

    confirmed = _is_confirmed_operator_tier(principal)
    redacted = [
        redact_settings_row(r, confirmed_operator_tier=confirmed)
        for r in rows
    ]

    if not redacted:
        message = "No project settings set (all toggles at defaults)."
    else:
        lines = [f"Project Settings ({len(redacted)} entries):"]
        for row in redacted:
            desc = f" — {row['description']}" if row.get("description") else ""
            lines.append(f"  • {row['context_key']} = {row['value']}{desc}")
        message = "\n".join(lines)

    return Ok(data={"settings": redacted}, message=message)


# --- update_project_settings ----------------------------------------------


@requires_capability("system.config.write")
async def update_project_settings_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Upsert a ``config_*`` row in the settings store.

    The ``system.config.write`` cap gate runs first (the decorator, so
    ``dispatch_tool_call`` denies an unauthorized caller before
    jsonschema even sees the arguments — R21-F1); the key must be
    ``config_*`` (Invalid otherwise — knowledge belongs in the
    project_context tools) is checked after, for an already-authorized
    caller. Value is JSON-encoded on write exactly like the context
    tools, keeping the coercion helpers' read contract unchanged.
    """
    context_key = arguments.get("context_key")
    context_value = arguments.get("context_value")
    description = arguments.get("description")
    description_provided = "description" in arguments

    if not context_key or not isinstance(context_key, str):
        return Invalid(field="context_key", message="context_key is required")
    if not _CONFIG_KEY_RE.match(context_key):
        return Invalid(
            field="context_key",
            message=(
                "project settings hold config_* keys only; use "
                "project_context tools for knowledge"
            ),
        )

    if context_value is None and "context_value" not in arguments:
        return Invalid(
            field="context_value", message="context_value is required"
        )

    try:
        value_json_str = json.dumps(context_value)
    except TypeError as e_type:
        return Invalid(
            field="context_value",
            message=(
                f"Provided context_value is not JSON serializable: {e_type}"
            ),
        )

    requesting_actor = _actor_label(principal)
    log_audit(
        requesting_actor,
        "update_project_settings",
        {"context_key": context_key},
    )

    try:
        with unit_of_work() as u:
            row, created = settings_repo.upsert(
                context_key,
                value_json_str,
                description,
                description_provided=description_provided,
                actor=requesting_actor,
                connection=u.cursor,
            )
            # Audit through the same cursor so the action lands in the
            # SAME transaction as the settings write.
            log_agent_action_to_db(
                u.cursor,
                requesting_actor,
                "updated_setting",
                details={"context_key": context_key, "created": created},
            )
    except sqlite3.Error as e:
        logger.error(
            f"Database error updating project setting '{context_key}': {e}",
            exc_info=True,
        )
        return Failed(message="Database error updating project settings")

    # BL-R14-1 parity: fire the same post-write wake set the context
    # tools fired for these keys before the cutover — worker-policy
    # toggle → tools/list_changed, loop toggle → wake_all_for_flag_recheck.
    await emit_context_write_wakes(context_key)

    return Ok(
        data={"context_key": context_key, "created": created},
        message=(
            f"Project setting "
            f"{'created' if created else 'updated'} for key "
            f"'{context_key}'."
        ),
    )


# --- delete_project_settings ----------------------------------------------


@requires_capability("system.config.write")
async def delete_project_settings_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Delete a ``config_*`` row from the settings store.

    Same gate as :func:`update_project_settings_tool_impl` (the
    ``system.config.write`` cap, now enforced by the decorator ahead of
    schema validation — R21-F1). Fires the post-write wakes for the
    deleted key too: a deleted toggle reverts to its default, so worker
    tool visibility / in-flight waiters may need to re-evaluate.
    """
    context_key = arguments.get("context_key")
    if not context_key or not isinstance(context_key, str):
        return Invalid(field="context_key", message="context_key is required")

    requesting_actor = _actor_label(principal)
    log_audit(
        requesting_actor,
        "delete_project_settings",
        {"context_key": context_key},
    )

    try:
        with unit_of_work() as u:
            deleted_rows = settings_repo.delete_many(
                [context_key], connection=u.cursor,
            )
            if not deleted_rows:
                return NotFound(
                    resource="project_settings", identifier=context_key,
                )
            log_agent_action_to_db(
                u.cursor,
                requesting_actor,
                "deleted_setting",
                details={"context_key": context_key},
            )
    except sqlite3.Error as e:
        logger.error(
            f"Database error deleting project setting '{context_key}': {e}",
            exc_info=True,
        )
        return Failed(message="Database error deleting project settings")

    # The deleted key reverts to its default — same wake seam as update.
    await emit_context_write_wakes(context_key)

    return Ok(
        data={"context_key": context_key},
        message=f"Project setting '{context_key}' deleted.",
    )


# --- registration -----------------------------------------------------------


_VALUE_ANYOF: List[Dict[str, Any]] = [
    {"type": "string"},
    {"type": "number"},
    {"type": "boolean"},
    {"type": "null"},
    {"type": "object", "additionalProperties": True},
    {"type": "array"},
]


def register_project_settings_tools() -> None:
    register_tool(
        name="view_project_settings",
        description=(
            "View the project's operational settings (config_* keys in "
            "the project_settings store). Operator-only; secret values "
            "are masked for unverifiable tiers."
        ),
        input_schema={
            "type": "object",
            "properties": {
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_project_settings_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="update_project_settings",
        description=(
            "Create or update a project setting (config_* key) in the "
            "project_settings store. Operator-only. Use the "
            "project_context tools for knowledge entries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "context_key": {
                    "type": "string",
                    "description": (
                        "The config_* key to set (e.g. "
                        "'config_allow_worker_to_worker')."
                    ),
                    "maxLength": IDENTIFIER_MAX_LEN,
                },
                "context_value": {
                    "description": (
                        "The JSON-serializable value to set (bool for "
                        "toggles, int for knobs, string for URLs/tokens)."
                    ),
                    "anyOf": _VALUE_ANYOF,
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of this setting.",
                },
            },
            "required": ["context_key", "context_value"],
            "additionalProperties": False,
        },
        implementation=update_project_settings_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="delete_project_settings",
        description=(
            "Delete a project setting (config_* key) from the "
            "project_settings store; the toggle reverts to its default. "
            "Operator-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "context_key": {
                    "type": "string",
                    "description": "The config_* key to delete.",
                    "maxLength": IDENTIFIER_MAX_LEN,
                },
            },
            "required": ["context_key"],
            "additionalProperties": False,
        },
        implementation=delete_project_settings_tool_impl,
        visibility="operator",
    )


# Call registration when this module is imported (same pattern as the
# sibling tool modules).
register_project_settings_tools()
