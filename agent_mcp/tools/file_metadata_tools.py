# Agent-MCP/mcp_template/mcp_server_src/tools/file_metadata_tools.py
"""File-metadata MCP tools.

Wave 6 PR 1 migration — both tools take a :class:`Principal` and
return :data:`ToolResult`. The legacy decorators are gone; the
admission moves into the impl:

* ``view_file_metadata`` — agent_bearer AND ``files.use``
  capability (SEC round-2 defense-in-depth; the legacy gate was
  ``@requires("any")`` / bare ``kind``).
* ``update_file_metadata`` — operator-tier only (matches the legacy
  ``@requires_role("operator")`` and the ``visibility="operator"``
  declaration on registration).

Operator-session callers calling ``view_file_metadata`` are not
admitted today because the metadata read had no use case for the
dashboard pre-Wave-6; PR 6 (or a UI-driven PR) can widen if needed.
"""
import json
import datetime
import sqlite3
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .registry import register_tool
from ..core.config import logger
from ..core.principal import Principal
from ..repositories import agent_repo
from ..core.tool_result import (
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..utils.audit_utils import log_audit
from ..db.connection import get_db_connection
from ..db.unit_of_work import unit_of_work
from ..db.actions.agent_actions_db import log_agent_action_to_db


def _normalize_filepath(
    filepath_arg: str, agent_id_for_wd: Optional[str]
) -> Optional[str]:
    """
    Resolves a filepath to an absolute, normalized POSIX path.
    Uses the agent's working directory if the path is relative.

    Returns ``None`` when the (tool-arg-derived) path can't be resolved —
    e.g. an embedded null byte makes ``Path.resolve()`` raise ``ValueError``.
    Callers map ``None`` to their existing ``Invalid`` path so a malformed
    path is a controlled 4xx-style tool error, not an unhandled 500
    (R6-F3 ``.resolve()``-ValueError class completion).
    """
    if not os.path.isabs(filepath_arg):
        working_dir = os.getcwd()  # Default to CWD if no agent context
        # PR-W2c: route through AgentRepository so cache misses fall
        # through to the DB row instead of dropping to CWD.
        if agent_id_for_wd:
            wd_from_repo = agent_repo.get_working_directory(agent_id_for_wd)
            if wd_from_repo:
                working_dir = wd_from_repo
            else:
                logger.warning(
                    f"Agent '{agent_id_for_wd}' not found in agent registry for path resolution. Using CWD."
                )

        resolved_path = Path(working_dir) / filepath_arg
    else:
        resolved_path = Path(filepath_arg)

    try:
        # Resolve to absolute and normalize to POSIX. ``.resolve()`` raises
        # ValueError on an embedded-null-byte path (and OSError on other bad
        # paths) — fail closed to None rather than propagate to a 500.
        return resolved_path.resolve().as_posix()
    except (ValueError, OSError):
        return None


# --- view_file_metadata tool ---
async def view_file_metadata_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    # SEC round-2 (defense-in-depth): gate on the ``files.use``
    # capability, not the bare ``kind`` (mirrors ``rag_tools.py`` under
    # SEC Wave-B / Finding 2). The prior ``kind == "agent_bearer"``
    # check admitted a bearer whose ``agent_role`` is None (empty
    # capability bundle). The ``kind`` check is retained so operator
    # sessions stay rejected — metadata working-dir resolution keys on
    # ``agent_id``, which operators don't carry; this read is
    # agent-only by design (the dashboard has no use case for it today).
    if (
        principal is None
        or principal.kind != "agent_bearer"
        or not principal.has_capability("files.use")
    ):
        return PermissionDenied(
            reason="agent token with files.use capability required to view file metadata"
        )

    filepath_arg = arguments.get("filepath")
    if not filepath_arg or not isinstance(filepath_arg, str):
        return Invalid(
            field="filepath",
            message="filepath is required and must be a string.",
        )

    requesting_agent_id = principal.agent_id or ""
    normalized_filepath_str = _normalize_filepath(filepath_arg, requesting_agent_id)
    if normalized_filepath_str is None:
        return Invalid(
            field="filepath",
            message="filepath could not be resolved to a valid path.",
        )

    log_audit(
        requesting_agent_id,
        "view_file_metadata",
        {"filepath_normalized": normalized_filepath_str, "original_path": filepath_arg},
    )

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT metadata, updated_by, last_updated, content_hash FROM file_metadata WHERE filepath = ?",
            (normalized_filepath_str,),
        )
        row = cursor.fetchone()
        if row is None:
            return NotFound(
                resource="file metadata",
                identifier=normalized_filepath_str,
            )
        try:
            metadata_parsed = json.loads(row["metadata"])
        except json.JSONDecodeError:
            logger.warning(
                f"Failed to parse JSON metadata for file "
                f"'{normalized_filepath_str}'. Raw: {row['metadata']}"
            )
            metadata_parsed = {
                "error": "Could not parse stored metadata string.",
                "raw_value": row["metadata"],
            }

        response_data = {
            "filepath": normalized_filepath_str,
            "metadata": metadata_parsed,
            "last_updated_by": row["updated_by"],
            "last_updated_at": row["last_updated"],
            "content_hash": (
                row["content_hash"] if "content_hash" in row.keys() else "N/A"
            ),
        }
        message = (
            f"Metadata for file '{filepath_arg}' (normalized: "
            f"{normalized_filepath_str}):\n\n"
            f"{json.dumps(response_data, indent=2, ensure_ascii=False)}"
        )
        return Ok(data=response_data, message=message)
    except sqlite3.Error as e_sql:
        logger.error(
            f"Database error viewing file metadata for "
            f"'{normalized_filepath_str}': {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error viewing file metadata: {e_sql}")
    except Exception as e:
        logger.error(
            f"Unexpected error viewing file metadata for "
            f"'{normalized_filepath_str}': {e}",
            exc_info=True,
        )
        return Failed(message=f"An unexpected error occurred: {e}")
    finally:
        if conn:
            conn.close()


# --- update_file_metadata tool ---
# Access table classifies this as "any" (workers see it in tools/list)
# but the impl gates operator-only. We preserve current enforcement
# (the access.py mismatch is a pre-existing visibility quirk;
# tightening tools/list would be a separate behavior-change PR).
async def update_file_metadata_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    # Wave 9 PR 3: gate on the operator-tier capability marker.
    # ``system.config.write`` is present in
    # PROJECT_ROLE_BUNDLES["operator"] and short-circuited by the
    # sysadmin wildcard; viewer-tier operators (read-only) lack the
    # cap and are correctly denied. Replaces the legacy
    # ``has_role("operator")`` which over-broadly admitted viewers.
    if principal is None or not principal.has_capability("system.config.write"):
        return PermissionDenied(
            reason="operator-tier authorization required to update file metadata"
        )

    filepath_arg = arguments.get("filepath")
    metadata_to_set = arguments.get("metadata")  # This is a Dict[str, Any]

    if not filepath_arg or not isinstance(filepath_arg, str):
        return Invalid(
            field="filepath",
            message="filepath is required and must be a string.",
        )
    if metadata_to_set is None or not isinstance(metadata_to_set, dict):
        return Invalid(
            field="metadata",
            message="metadata is required and must be a dictionary.",
        )

    # Operator-tier callers attribute via user_id; if a manager-role
    # agent ever carries ``system.config.write`` (e.g. via a group
    # cap grant in a future widening), principal.agent_id would slot
    # in via the same actor_label call.
    requesting_admin_id = principal.actor_label()

    normalized_filepath_str = _normalize_filepath(
        filepath_arg, principal.agent_id
    )
    if normalized_filepath_str is None:
        return Invalid(
            field="filepath",
            message="filepath could not be resolved to a valid path.",
        )

    log_audit(
        requesting_admin_id,
        "update_file_metadata",
        {
            "filepath_normalized": normalized_filepath_str,
            "original_path": filepath_arg,
            "metadata_keys": list(metadata_to_set.keys()),
        },
    )

    try:
        metadata_json_str = json.dumps(metadata_to_set)
    except TypeError as e_type:
        logger.error(
            f"Metadata provided for file '{normalized_filepath_str}' is "
            f"not JSON serializable: {e_type}"
        )
        return Invalid(
            field="metadata",
            message=f"Provided metadata is not JSON serializable: {e_type}",
        )

    try:
        # D3: the unit-of-work owns the transaction. The metadata write
        # AND its ``updated_file_metadata`` DB-audit row run on the
        # scope's cursor so they commit (or roll back) atomically —
        # exactly the pairing the hand-sequenced ``conn.commit()`` gave,
        # now structural. The in-memory ``log_audit`` sink stays above
        # (pre-write, present-tense ``update_file_metadata`` action with
        # its richer detail bag) so both the DB and in-memory audit
        # records keep their exact historical content.
        with unit_of_work() as u:
            cursor = u.cursor
            updated_at_iso = datetime.datetime.now().isoformat()

            cursor.execute(
                """
                INSERT OR REPLACE INTO file_metadata (filepath, metadata, last_updated, updated_by)
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_filepath_str,
                    metadata_json_str,
                    updated_at_iso,
                    requesting_admin_id,
                ),
            )

            log_agent_action_to_db(
                cursor,
                requesting_admin_id,
                "updated_file_metadata",
                details={"filepath": normalized_filepath_str, "action": "set/update"},
            )

            logger.info(
                f"File metadata for '{normalized_filepath_str}' updated by "
                f"'{requesting_admin_id}'."
            )
            # Returning here runs the uow __exit__: commit the INSERT +
            # audit row together.
            return Ok(
                data={
                    "filepath": normalized_filepath_str,
                    "original_path": filepath_arg,
                    "updated_by": requesting_admin_id,
                    "last_updated": updated_at_iso,
                },
                message=(
                    f"File metadata updated successfully for "
                    f"'{filepath_arg}' (normalized: {normalized_filepath_str})."
                ),
            )

    except sqlite3.Error as e_sql:
        # The unit-of-work already rolled back + closed the connection.
        logger.error(
            f"Database error updating file metadata for "
            f"'{normalized_filepath_str}': {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error updating file metadata: {e_sql}")
    except Exception as e:
        logger.error(
            f"Unexpected error updating file metadata for "
            f"'{normalized_filepath_str}': {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error updating file metadata: {e}")


# --- Register file metadata tools ---
def register_file_metadata_tools():
    register_tool(
        name="view_file_metadata",  # main.py:1841 (schema name)
        description="View stored metadata (e.g., purpose, components) for a specific file path.",
        input_schema={  # From main.py:1842-1852
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "filepath": {
                    "type": "string",
                    "description": "Path to the file (can be relative to agent's CWD or absolute)",
                },
            },
            "required": ["filepath"],
            "additionalProperties": False,
        },
        implementation=view_file_metadata_tool_impl,
    )

    register_tool(
        name="update_file_metadata",  # main.py:1854 (schema name)
        description="Add or replace the entire metadata object for a specific file path. Admin only.",
        input_schema={  # From main.py:1855-1867
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "filepath": {
                    "type": "string",
                    "description": "Path to the file (can be relative or absolute)",
                },
                "metadata": {
                    "type": "object",
                    "description": "A JSON object containing the metadata to set for the file.",
                },
            },
            "required": ["filepath", "metadata"],
            "additionalProperties": False,
        },
        implementation=update_file_metadata_tool_impl,
        # Operator-only at call-time (impl checks
        # ``principal.has_capability("system.config.write")``); the
        # old hand-maintained TOOL_ACCESS classified this "any" (a
        # pre-existing visibility quirk: workers saw it in tools/list
        # but the call always failed). PR-W1c aligns visibility with
        # call-time enforcement.
        visibility="operator",
    )


# Call registration when this module is imported
register_file_metadata_tools()
