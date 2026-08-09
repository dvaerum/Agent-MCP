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
edit/delete it. Manager-tier admits any operator-tier
:class:`Principal` (operator session or forwarding header) OR an
agent token whose row has ``agent_role='manager'``. The per-note
ownership check happens inside the impl against
``task_notes_db.edit_note`` / ``delete_note``, which still takes
the historical ``is_admin`` boolean (Wave 9 PR 3: sourced from
``principal.has_capability("tasks.assign")`` — the manager-tier
marker present in both ``PROJECT_ROLE_BUNDLES["operator"]`` and
``AGENT_ROLE_BUNDLES["manager"]``).

The existing append-only writers in `task_tools.py` (the bulk
add_note operation, the inline notes append in
update_task_status_tool_impl, the initial-note inserts) still
mutate the legacy `tasks.notes` JSON column for the deprecation
window. A follow-up PR flips those over and drops the JSON column.

Wave 6 PR 1 — all three tools are now on the Principal +
ToolResult signature. The author/requester is sourced from
``principal.agent_id or principal.user_id`` so both bearer-authed
agents and operator-session callers attribute correctly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.principal import Principal
from ..core.tool_result import (
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..db.actions import task_notes_db
from ..repositories.task_repository import get_task_by_id
from .registry import register_tool


# Static author-only policy clause appended to the fused NotFound (see
# _classify_db_error). It ADDS the actionable "who may edit/delete"
# hint WITHOUT naming the note's author or confirming the note exists —
# a worker looking at a note that is plainly present in ``view_tasks``
# but authored by someone else no longer reads a bare "not found" as a
# bug. Leading ", " continues the "<resource> '<id>' not found" clause
# the NotFound renderer emits; kept STATIC (no author interpolation) so
# it is safe to show on both the missing-note and foreign-note branches.
_AUTHOR_ONLY_HINT = (
    ", or you are not its author. Only a note's original author "
    "(or an admin) can edit or delete it."
)


def _classify_db_error(err: str, note_id: int) -> ToolResult:
    """Map ``task_notes_db.edit_note`` / ``delete_note`` error
    strings onto the typed :data:`ToolResult` variants.

    The DB layer returns ``(False, free_form_error_string)``; we
    text-match on stable substrings to produce typed results:

    * ``"not found"`` → :class:`NotFound` (REST → 404)
    * ``"owned by"`` (ownership failure) → :class:`NotFound` — see
      SECURITY below
    * anything else (DB error) → :class:`Failed` (REST → 500)

    Both the missing-note and ownership branches return the SAME
    ``NotFound`` carrying :data:`_AUTHOR_ONLY_HINT`, so the caller sees
    one opaque message — ``"task note '<id>' not found, or you are not
    its author. Only a note's original author (or an admin) can edit or
    delete it."`` — whichever outcome actually occurred.

    SECURITY (PF-1): the ownership-failure path must be INDISTINGUISHABLE
    from the missing-note path. Otherwise a worker holding a foreign
    ``note_id`` can tell "note exists but isn't yours" apart from "no
    such note" — a note-existence oracle. The fused message must not
    confirm the note exists NOR name the owner: we drop the DB layer's
    ``"owned by {author!r}"`` string entirely and add only the STATIC
    author-only policy hint. A manager-tier caller never reaches the
    ownership branch (``is_admin=True`` bypasses the check in the DB
    layer), so the fused wording only affects non-owner workers.

    Centralised so the two callers (edit, delete) classify
    consistently and the contract with the DB layer is documented
    in one place.
    """
    low = err.lower()
    if "not found" in low or "owned by" in low:
        return NotFound(
            resource="task note",
            identifier=str(note_id),
            hint=_AUTHOR_ONLY_HINT,
        )
    return Failed(message=err)


async def add_task_note_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Wave 6 PR 0 demo + PR 1 family.

    Policy (SEC Wave-B, per-task ownership): a note author must be the
    target task's assignee or creator, OR a manager-tier caller
    (``tasks.assign`` — operator sessions, forwarding headers, and
    manager-role agents; short-circuited by the sysadmin wildcard). A
    worker may annotate only its own tasks; a manager / operator may
    annotate any. Notes to a nonexistent task are rejected — task
    notes feed other agents' / the operator's LLM context, so an
    unrelated bearer writing into a foreign (or phantom) task's notes
    is a cross-agent stored-injection primitive (same class as the
    viewer project_context write already fixed).
    """
    if principal is None or (
        principal.kind != "agent_bearer"
        and not principal.has_capability("system.config.write")
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

    # Per-task ownership gate. Reject phantom tasks (no orphan notes)
    # and, for non-manager callers, require the caller to be the task's
    # assignee or creator. ``tasks.assign`` is the manager-tier marker
    # (present in PROJECT_ROLE_BUNDLES["operator"] AND
    # AGENT_ROLE_BUNDLES["manager"], short-circuited by the sysadmin
    # wildcard) — the same marker the edit/delete note tools use.
    task = get_task_by_id(task_id)
    if task is None:
        return NotFound(resource="task", identifier=str(task_id))
    requester = principal.agent_id or principal.user_id or ""
    if not principal.has_capability("tasks.assign"):
        owners = {task.get("assigned_to"), task.get("created_by")}
        if not requester or requester not in owners:
            # SECURITY (PF-1): for a FOREIGN-owned task return the SAME
            # not-found result the phantom-task branch above returns, so
            # a non-owner worker cannot use the 403-vs-404 shape to
            # confirm a foreign task exists, and never interpolate the
            # owner's identity.
            #
            # An UNASSIGNED task (``assigned_to`` NULL/empty) has no owner
            # to hide and is already publicly listed in the claimable pool
            # via view_tasks (#515) — so instead of stranding a worker
            # that wants to annotate pool work, guide it to self-claim the
            # task first. ``_worker_ownership_deny_result`` (shared with
            # _update_single_task / request_assistance) returns the
            # actionable "claim it first" PermissionDenied for the
            # unassigned case and the UNCHANGED phantom-404 for the
            # foreign-owned case. Lazy import avoids a heavy/cyclic
            # module-load dependency on task_tools.
            from .task_tools import _worker_ownership_deny_result

            return _worker_ownership_deny_result(
                str(task_id),
                task.get("assigned_to"),
                action="add a note to it",
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
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Edit a task note.

    Policy: any authenticated principal may attempt; ``is_admin``
    governs whether the per-note ownership check is bypassed.
    Manager-tier callers (operator session, forwarding header, or
    manager-role agent) get ``is_admin=True`` and can moderate
    worker-authored notes. Worker agents must be the original
    author.
    """
    if principal is None or (
        principal.kind != "agent_bearer"
        and not principal.has_capability("system.config.write")
    ):
        return PermissionDenied(
            reason="agent or operator token required to edit a task note"
        )

    note_id_raw = arguments.get("note_id")
    new_text = arguments.get("text")
    if note_id_raw is None:
        return Invalid(field="note_id", message="`note_id` is required.")
    if not new_text:
        return Invalid(field="text", message="`text` is required.")
    try:
        note_id = int(note_id_raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError (R16 sweep): ``int(float('inf'))`` from a JSON
        # ``1e400`` token — sibling of the scheduler R16-F3/F4 fix.
        return Invalid(
            field="note_id",
            message=f"`note_id` must be an integer, got {note_id_raw!r}.",
        )

    # Requester for the per-note ownership check + is_admin source.
    # Manager-tier (operators or manager-role agents) bypass the
    # author check via is_admin=True; workers must be the author.
    requester = principal.agent_id or principal.user_id or ""
    # Wave 9 PR 3: ``tasks.assign`` is the manager-tier marker —
    # present in PROJECT_ROLE_BUNDLES["operator"] AND
    # AGENT_ROLE_BUNDLES["manager"], short-circuited by the sysadmin
    # wildcard. Replaces ``has_role("manager")``; same admit set
    # (operator + sysadmin + manager-role agent) in the cap model.
    is_admin = principal.has_capability("tasks.assign")

    ok, err = task_notes_db.edit_note(
        note_id=note_id,
        requester=requester,
        new_text=new_text,
        is_admin=is_admin,
    )
    if not ok:
        return _classify_db_error(err, note_id)
    return Ok(
        data={"note_id": note_id},
        message=f"Note {note_id} updated.",
    )


async def delete_task_note_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Delete a task note. Same ownership/moderation contract as
    :func:`edit_task_note_tool_impl`."""
    if principal is None or (
        principal.kind != "agent_bearer"
        and not principal.has_capability("system.config.write")
    ):
        return PermissionDenied(
            reason="agent or operator token required to delete a task note"
        )

    note_id_raw = arguments.get("note_id")
    if note_id_raw is None:
        return Invalid(field="note_id", message="`note_id` is required.")
    try:
        note_id = int(note_id_raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError (R16 sweep): ``int(float('inf'))`` from a JSON
        # ``1e400`` token — sibling of the scheduler R16-F3/F4 fix.
        return Invalid(
            field="note_id",
            message=f"`note_id` must be an integer, got {note_id_raw!r}.",
        )

    requester = principal.agent_id or principal.user_id or ""
    # Wave 9 PR 3: ``tasks.assign`` is the manager-tier marker —
    # present in PROJECT_ROLE_BUNDLES["operator"] AND
    # AGENT_ROLE_BUNDLES["manager"], short-circuited by the sysadmin
    # wildcard. Replaces ``has_role("manager")``; same admit set
    # (operator + sysadmin + manager-role agent) in the cap model.
    is_admin = principal.has_capability("tasks.assign")

    ok, err = task_notes_db.delete_note(
        note_id=note_id, requester=requester, is_admin=is_admin,
    )
    if not ok:
        return _classify_db_error(err, note_id)
    return Ok(
        data={"note_id": note_id},
        message=f"Note {note_id} deleted.",
    )


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
            "edited/deleted by the original author or admin. You must own "
            "(be assigned to, or have created) the task; if it is unassigned "
            "(in the claimable pool), claim it first with "
            "assign_task(task_ids=[...], agent_token=<your own>)."
        ),
        input_schema={
            "type": "object",
            "properties": {
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
                "note_id": {
                    "type": "integer",
                    "description": "Side-table note_id to edit.",
                    # PF-R39-1: clamp to sqlite's signed-64-bit range.
                    # note_id is a positive autoincrement PK, so a value
                    # outside [1, 2^63-1] can never match a real row —
                    # and binding an out-of-range int makes the sqlite3
                    # driver raise a bare OverflowError. Reject it at
                    # dispatch (clean -32602) before it reaches the DB.
                    "minimum": 1,
                    "maximum": 9223372036854775807,
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
                "note_id": {
                    "type": "integer",
                    "description": "Side-table note_id to delete.",
                    # PF-R39-1: see edit_task_note above — clamp to
                    # sqlite's signed-64-bit range so an out-of-range id
                    # is rejected at dispatch instead of overflowing the
                    # sqlite3 driver's int bind.
                    "minimum": 1,
                    "maximum": 9223372036854775807,
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
