# Agent-MCP/mcp_template/mcp_server_src/tools/file_management_tools.py
"""File-claim / file-status MCP tools.

Wave 6 PR 1 migration — both tools take a :class:`Principal` and
return :data:`ToolResult`. Admission is agent-only AND
capability-gated: the caller must be an ``agent_bearer`` AND carry
the ``files.use`` capability (SEC round-2 defense-in-depth; see the
per-tool comments). Operator-session callers are not admitted
because the file map is keyed on agent identity (claim / release /
lookup are all per-agent verbs) and an operator session doesn't
have an ``agent_id``; empty-capability bearers (``agent_role`` None)
are likewise denied by the ``files.use`` check.
"""
import os
import datetime
from typing import Any, Dict, Optional

from .registry import register_tool
from ..core.config import logger
from ..core import globals as g
from ..core.principal import Principal
from ..repositories import agent_repo
from ..core.tool_result import (
    Conflict,
    Invalid,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..utils.audit_utils import log_audit
# No DB interactions for these specific tools as they manage in-memory state (g.file_map)

# --- check_file_status tool ---
async def check_file_status_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    # SEC round-2 (defense-in-depth): gate on the ``files.use``
    # capability, not the bare ``kind`` (mirrors ``rag_tools.py`` under
    # SEC Wave-B / Finding 2). The prior ``kind == "agent_bearer"``
    # check admitted a bearer whose ``agent_role`` is None (empty
    # capability bundle). The ``kind`` check is retained so operator
    # sessions stay rejected — the in-memory file map keys on
    # ``agent_id``, which operators don't carry; this tool is
    # agent-only by design.
    if (
        principal is None
        or principal.kind != "agent_bearer"
        or not principal.has_capability("files.use")
    ):
        return PermissionDenied(
            reason="agent token with files.use capability required to check file status"
        )

    filepath_arg = arguments.get("filepath")
    if not filepath_arg or not isinstance(filepath_arg, str):
        return Invalid(
            field="filepath",
            message="filepath is required and must be a string.",
        )

    requesting_agent_id = principal.agent_id or ""

    # Resolve the filepath to absolute path.
    # PR-W2c: routed through AgentRepository.get_working_directory()
    # so a cache miss falls through to the DB row instead of silently
    # falling back to server CWD.
    if not os.path.isabs(filepath_arg):
        agent_wd = agent_repo.get_working_directory(requesting_agent_id)
        if not agent_wd:
            # This case should ideally not happen if agent is properly initialized
            logger.warning(
                f"Agent '{requesting_agent_id}' has no working directory "
                f"recorded. Using current server CWD as fallback for path "
                f"resolution."
            )
            agent_wd = os.getcwd()
        resolved_abs_filepath = os.path.abspath(os.path.join(agent_wd, filepath_arg))
    else:
        resolved_abs_filepath = os.path.abspath(filepath_arg)

    log_audit(
        requesting_agent_id,
        "check_file_status",
        {"filepath": resolved_abs_filepath, "original_path": filepath_arg},
    )

    if resolved_abs_filepath in g.file_map:
        file_info = g.file_map[resolved_abs_filepath]
        if file_info.get("agent_id") == requesting_agent_id:
            message = (
                f"File '{filepath_arg}' (resolved: {resolved_abs_filepath}) is currently "
                f"being used by YOU ({requesting_agent_id}) since {file_info.get('timestamp', 'N/A')}. "
                f"Status: {file_info.get('status', 'unknown')}"
            )
        else:
            message = (
                f"File '{filepath_arg}' (resolved: {resolved_abs_filepath}) is currently "
                f"being used by agent '{file_info.get('agent_id', 'unknown')}' "
                f"since {file_info.get('timestamp', 'N/A')}. Status: {file_info.get('status', 'unknown')}"
            )
        return Ok(
            data={
                "filepath": resolved_abs_filepath,
                "original_path": filepath_arg,
                "in_use": True,
                "agent_id": file_info.get("agent_id"),
                "status": file_info.get("status"),
                "timestamp": file_info.get("timestamp"),
            },
            message=message,
        )
    # Free files surface as Ok(in_use=False) rather than NotFound —
    # NotFound is reserved for "the named resource doesn't exist",
    # but here the resource (the path) is well-formed and the
    # information being asked for ("is anyone holding it?") has a
    # definitive negative answer.
    return Ok(
        data={
            "filepath": resolved_abs_filepath,
            "original_path": filepath_arg,
            "in_use": False,
        },
        message=(
            f"File '{filepath_arg}' (resolved: {resolved_abs_filepath}) is "
            f"not currently being used by any agent according to the file map."
        ),
    )

# --- update_file_status tool ---
async def update_file_status_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    # SEC round-2 (defense-in-depth): gate on the ``files.use``
    # capability, not the bare ``kind`` (see check_file_status above).
    if (
        principal is None
        or principal.kind != "agent_bearer"
        or not principal.has_capability("files.use")
    ):
        return PermissionDenied(
            reason="agent token with files.use capability required to update file status"
        )

    filepath_arg = arguments.get("filepath")
    new_status = arguments.get("status")  # e.g., "editing", "reading", "released"

    if not filepath_arg or not isinstance(filepath_arg, str):
        return Invalid(
            field="filepath",
            message="filepath is required and must be a string.",
        )
    if not new_status or not isinstance(new_status, str):
        return Invalid(
            field="status",
            message="status is required and must be a string.",
        )

    requesting_agent_id = principal.agent_id or ""

    # Resolve the filepath to absolute path.
    # PR-W2c: routed through AgentRepository.get_working_directory().
    if not os.path.isabs(filepath_arg):
        agent_wd = agent_repo.get_working_directory(requesting_agent_id)
        if not agent_wd:
            logger.warning(
                f"Agent '{requesting_agent_id}' has no working directory "
                f"recorded. Using CWD for path resolution."
            )
            agent_wd = os.getcwd()
        resolved_abs_filepath = os.path.abspath(os.path.join(agent_wd, filepath_arg))
    else:
        resolved_abs_filepath = os.path.abspath(filepath_arg)

    valid_statuses = ["editing", "reading", "reviewing", "released"]
    if new_status not in valid_statuses:
        return Invalid(
            field="status",
            message=(
                f"Invalid status: '{new_status}'. Must be one of: "
                f"{', '.join(valid_statuses)}"
            ),
        )

    # Ownership gate: only the holder may mutate a foreign-held lock —
    # claim (editing/reading/reviewing) OR release. SEC-R20 (AZ-R20-1):
    # the prior guard carved out ``and new_status != "released"`` so
    # "Can always release, even if map is out of sync." That let a
    # NON-holder call ``update_file_status(release)`` on another
    # agent's file and the release branch below unconditionally
    # ``del``'d the entry — a cross-agent advisory-lock STEAL (same
    # foreign-object-mutation class the R19 task-tools sweep gated).
    # Dropping the carve-out routes a non-holder release to the SAME
    # Conflict a non-holder claim gets. The holder still releases their
    # own lock: ``agent_id != requesting_agent_id`` is false when the
    # requester IS the holder, so this guard doesn't fire for
    # self-release. Operators never reach this tool (it is gated to
    # ``kind == "agent_bearer"``), so there is no operator/admin
    # force-release affordance to preserve here.
    if (
        resolved_abs_filepath in g.file_map
        and g.file_map[resolved_abs_filepath].get("agent_id") != requesting_agent_id
    ):
        current_holder_agent_id = g.file_map[resolved_abs_filepath].get(
            "agent_id", "another agent"
        )
        # Another agent holds the claim. Conflict (HTTP 409) rather
        # than PermissionDenied — the caller's principal is fine,
        # the state of the file map blocks the operation.
        verb = "release" if new_status == "released" else "claim"
        return Conflict(
            reason=(
                f"File '{filepath_arg}' (resolved: {resolved_abs_filepath}) "
                f"is already being used by agent "
                f"'{current_holder_agent_id}'. Cannot {verb} it with status "
                f"'{new_status}'."
            )
        )

    # Update the file map (g.file_map)
    if new_status == "released":
        if resolved_abs_filepath in g.file_map:
            # Reached only when the requester IS the holder (the
            # ownership gate above returns Conflict for a foreign
            # holder) — self-release of one's own advisory lock.
            del g.file_map[resolved_abs_filepath]
            log_audit(
                requesting_agent_id,
                "release_file",
                {"filepath": resolved_abs_filepath, "original_path": filepath_arg},
            )
            logger.info(
                f"Agent '{requesting_agent_id}' released file "
                f"'{resolved_abs_filepath}'."
            )
            return Ok(
                data={
                    "filepath": resolved_abs_filepath,
                    "original_path": filepath_arg,
                    "status": "released",
                },
                message=(
                    f"File '{filepath_arg}' (resolved: "
                    f"{resolved_abs_filepath}) has been released."
                ),
            )
        # File was not in map — the original surfaced this as an
        # informational success ("already considered released or
        # never tracked"), not an error, because releasing an
        # untracked path is idempotent from the caller's point of
        # view. Preserve that semantic with Ok(in_use=False).
        log_audit(
            requesting_agent_id,
            "attempt_release_unmapped_file",
            {"filepath": resolved_abs_filepath, "original_path": filepath_arg},
        )
        return Ok(
            data={
                "filepath": resolved_abs_filepath,
                "original_path": filepath_arg,
                "in_use": False,
            },
            message=(
                f"File '{filepath_arg}' (resolved: "
                f"{resolved_abs_filepath}) was not found in the active "
                f"file map (already considered released or never "
                f"tracked)."
            ),
        )

    # For "editing", "reading", "reviewing":
    g.file_map[resolved_abs_filepath] = {
        "agent_id": requesting_agent_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "status": new_status,
    }
    log_audit(
        requesting_agent_id,
        f"claim_file_{new_status}",
        {"filepath": resolved_abs_filepath, "original_path": filepath_arg},
    )
    logger.info(
        f"Agent '{requesting_agent_id}' updated file "
        f"'{resolved_abs_filepath}' status to '{new_status}'."
    )
    return Ok(
        data={
            "filepath": resolved_abs_filepath,
            "original_path": filepath_arg,
            "agent_id": requesting_agent_id,
            "status": new_status,
        },
        message=(
            f"File '{filepath_arg}' (resolved: {resolved_abs_filepath}) "
            f"is now registered to agent '{requesting_agent_id}' with "
            f"status '{new_status}'."
        ),
    )

# --- Register file management tools ---
def register_file_management_tools():
    register_tool(
        name="check_file_status", # main.py:1825
        description="Check if a file is currently being used by another agent, based on the server's in-memory file map.",
        input_schema={ # From main.py:1826-1839
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Agent authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "filepath": {"type": "string", "description": "Path to the file to check (can be relative to agent's CWD or absolute)"}
            },
            "required": ["filepath"],
            "additionalProperties": False
        },
        implementation=check_file_status_tool_impl
    )

    register_tool(
        name="update_file_status", # main.py:1841
        description="Update the status of a file in the server's in-memory map (e.g., claim for editing, reading, or release it).",
        input_schema={ # From main.py:1842-1858
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Agent authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "filepath": {"type": "string", "description": "Path to the file to update (can be relative or absolute)"},
                "status": {
                    "type": "string",
                    "description": "New status for the file.",
                    "enum": ["editing", "reading", "reviewing", "released"]
                }
            },
            "required": ["filepath", "status"],
            "additionalProperties": False
        },
        implementation=update_file_status_tool_impl
    )

# Call registration when this module is imported
register_file_management_tools()
