"""MCP tools for the `task_notes` side table (db-review PR-H).

Per-note edit/delete was the chief limitation called out by
PR #74's caveat — appending into the JSON list in `tasks.notes`
worked, but you couldn't target a single note for mutation. The
side table (migration 0009 / model TaskNote) fixes that; this
module wires it up as three MCP tools:

    add_task_note(task_id, text) -> note_id
    edit_task_note(note_id, text)
    delete_task_note(note_id)

Authoring contract (matches the dashboard agent-actions log
convention): only the note's author or a manager-tier+ caller may
edit/delete it. Manager-tier admits the system bearer OR an agent
token whose row has ``agent_role='manager'`` — see
``verify_token(token, "manager")`` (Phase 2 Wave 2a). The
``requires`` decorator only gates "can this token call the tool at
all?"; the per-note ownership check happens inside the impl against
``task_notes_db.edit_note`` / ``delete_note``, which still takes
the historical ``is_admin`` boolean (now sourced from the
manager-tier check).

The ``is_admin`` source is ``verify_token(token, "manager")`` so a
manager-role agent can moderate worker notes.

The existing append-only writers in `task_tools.py` (the bulk
add_note operation, the inline notes append in
update_task_status_tool_impl, the initial-note inserts) still
mutate the legacy `tasks.notes` JSON column for the deprecation
window. A follow-up PR flips those over and drops the JSON column.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import mcp.types as mcp_types

from ..core.auth import get_agent_id, verify_token
from ..core.principal import Principal
from ..core.tool_result import (
    Failed,
    Invalid,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..db.actions import task_notes_db
from .registry import register_tool


def _resolve_caller(arguments: Dict[str, Any]) -> tuple[str, str, bool]:
    """Pull token out of `arguments` and resolve
    (token, agent_id, is_manager_or_above).

    Mirrors the auth pattern used by the other task tools: tokens
    may arrive via `token` arg or, when run through the MCP stream
    that already verified the Authorization header, are surfaced as
    `_bearer_token`. Returns `("", "", False)` if neither is set.

    The "is admin" boolean is sourced from
    ``verify_token(token, "manager")`` (agent tokens whose row has
    ``agent_role='manager'``). The variable name stays ``is_admin``
    because that's what downstream ``task_notes_db.edit_note`` /
    ``delete_note`` accept.
    """
    token = (
        arguments.get("_bearer_token")
        or arguments.get("token")
        or ""
    )
    agent_id = get_agent_id(token) or ""
    is_admin = verify_token(token, required_role="manager")
    return token, agent_id, is_admin


async def add_task_note_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """E2E migration demo for Wave 6 PR 0.

    First tool to take a typed :class:`Principal` and return a
    :class:`ToolResult`. The bridge in ``dispatch_tool_call``
    synthesizes a Principal from ContextVars when one isn't passed
    (every pre-Wave-6 call site), so this signature is back-compat
    with the unmigrated dispatcher path during PRs 1-5. PR 6
    flips ``principal`` to a required kwarg and removes the
    fallback.

    Policy: any authenticated principal can author a note. Operator
    sessions count (the dashboard adds notes on the operator's
    behalf); any agent_bearer counts (workers + managers).
    """
    if principal is None or (
        principal.kind != "agent_bearer"
        and not principal.has_role("operator")
    ):
        return PermissionDenied(
            reason="agent or operator token required to add a task note"
        )

    task_id = arguments.get("task_id")
    text = arguments.get("text")
    if not task_id:
        return Invalid(
            field="task_id",
            message="`task_id` is required.",
        )
    if not text:
        return Invalid(
            field="text",
            message="`text` is required.",
        )

    # Author attribution: agent_bearer → agent_id; operator path →
    # user_id (the operator's username from the session row). The
    # task_notes_db.add_note column is a free-form string already, so
    # the operator label slots in next to legacy agent_id entries
    # without a schema change.
    author = principal.agent_id or principal.user_id

    note_id = task_notes_db.add_note(
        task_id=task_id, author=author, text=text,
    )
    if note_id is None:
        return Failed(message=f"Failed to add note to task '{task_id}'.")
    return Ok(
        data={"note_id": note_id, "task_id": task_id},
        message=f"Note {note_id} added to task '{task_id}'.",
    )


async def edit_task_note_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    token, agent_id, is_admin = _resolve_caller(arguments)
    if not verify_token(token, required_role="agent"):
        return [
            mcp_types.TextContent(
                type="text",
                text="Error: Unauthorized. Valid agent or admin token required.",
            )
        ]

    note_id_raw = arguments.get("note_id")
    new_text = arguments.get("text")
    if note_id_raw is None or not new_text:
        return [
            mcp_types.TextContent(
                type="text",
                text="Error: Both `note_id` and `text` are required.",
            )
        ]
    try:
        note_id = int(note_id_raw)
    except (TypeError, ValueError):
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Error: `note_id` must be an integer, got {note_id_raw!r}.",
            )
        ]

    ok, err = task_notes_db.edit_note(
        note_id=note_id,
        requester=agent_id,
        new_text=new_text,
        is_admin=is_admin,
    )
    if not ok:
        return [mcp_types.TextContent(type="text", text=f"Error: {err}")]
    return [
        mcp_types.TextContent(type="text", text=f"Note {note_id} updated.")
    ]


async def delete_task_note_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    token, agent_id, is_admin = _resolve_caller(arguments)
    if not verify_token(token, required_role="agent"):
        return [
            mcp_types.TextContent(
                type="text",
                text="Error: Unauthorized. Valid agent or admin token required.",
            )
        ]

    note_id_raw = arguments.get("note_id")
    if note_id_raw is None:
        return [
            mcp_types.TextContent(
                type="text", text="Error: `note_id` is required.",
            )
        ]
    try:
        note_id = int(note_id_raw)
    except (TypeError, ValueError):
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Error: `note_id` must be an integer, got {note_id_raw!r}.",
            )
        ]

    ok, err = task_notes_db.delete_note(
        note_id=note_id, requester=agent_id, is_admin=is_admin,
    )
    if not ok:
        return [mcp_types.TextContent(type="text", text=f"Error: {err}")]
    return [
        mcp_types.TextContent(type="text", text=f"Note {note_id} deleted.")
    ]


def register_task_notes_tools() -> None:
    """Register the three side-table tools.

    All three are `"any"` in tools/access.py — gated only by valid
    token; ownership is enforced inside the impl.
    """
    register_tool(
        name="add_task_note",
        description=(
            "Add a note to a task via the side table (db-review PR-H). "
            "Returns the new note_id. Notes added this way can be "
            "edited/deleted by the original author or admin."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "Authentication token. Optional if "
                        "Authorization: Bearer header is supplied."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": "Task to attach the note to.",
                },
                "text": {
                    "type": "string",
                    "description": "Note text.",
                },
            },
            "required": ["task_id", "text"],
            "additionalProperties": False,
        },
        implementation=add_task_note_tool_impl,
    )
    register_tool(
        name="edit_task_note",
        description=(
            "Edit a task note (db-review PR-H side table). Only the "
            "original author or admin may edit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "Authentication token. Optional if "
                        "Authorization: Bearer header is supplied."
                    ),
                },
                "note_id": {
                    "type": "integer",
                    "description": "Side-table note_id to edit.",
                },
                "text": {
                    "type": "string",
                    "description": "Replacement note text.",
                },
            },
            "required": ["note_id", "text"],
            "additionalProperties": False,
        },
        implementation=edit_task_note_tool_impl,
    )
    register_tool(
        name="delete_task_note",
        description=(
            "Delete a task note (db-review PR-H side table). Only the "
            "original author or admin may delete."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "Authentication token. Optional if "
                        "Authorization: Bearer header is supplied."
                    ),
                },
                "note_id": {
                    "type": "integer",
                    "description": "Side-table note_id to delete.",
                },
            },
            "required": ["note_id"],
            "additionalProperties": False,
        },
        implementation=delete_task_note_tool_impl,
    )


# Call registration when this module is imported (matches the
# pattern used by every other tool module in this package).
register_task_notes_tools()
