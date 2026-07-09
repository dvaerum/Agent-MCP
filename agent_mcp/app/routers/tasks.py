"""Tasks resource router — ``/api/tasks/...``.

Wave 8 PR 1 of prancy-napping-pie: the task handlers mechanically
moved out of ``app/routes.py`` onto this router:
``all_tasks``, ``create_task``, ``delete_task``.

The ``update_task_details`` handler lives on the ``composition``
router (not on this router) because its URL is
``/api/update-task-dashboard`` — it doesn't match the
``/api/tasks`` prefix. URL stability wins; a future PR can migrate
the URL to ``/api/tasks/{task_id}`` PATCH alongside dashboard
updates.

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR — ``GET /api/tasks`` is
currently open today and hoisting the gate to the router would
silently flip its auth behavior.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _dispatch_through_tool, handle_options
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(
    prefix="/api/tasks",
    tags=["tasks"],
)


@router.api_route("", methods=["GET", "OPTIONS"])
async def all_tasks_api_route(request: Request) -> JSONResponse:
    # GET /api/tasks[?assigned_to=<agent_id>][?unassigned=true]
    #
    # Default (no query params): returns every task row (back-compat
    # with the existing dashboard listing).
    #
    # `?assigned_to=<agent_id>` filters to tasks whose assigned_to
    # column matches exactly. Replaces the router's `list_tasks_for`
    # synthetic.
    #
    # `?unassigned=true` filters to tasks with assigned_to IS NULL.
    # Replaces the router's `list_unassigned_tasks` synthetic. If both
    # params are supplied, `unassigned=true` wins (mutually exclusive
    # by nature — IS NULL never matches a literal agent_id).
    #
    # Phase 7c, Q7.2 in plan.
    if request.method == 'OPTIONS':
        return await handle_options(request)

    assigned_to_filter: Optional[str] = request.query_params.get('assigned_to')
    unassigned_raw = request.query_params.get('unassigned', '')
    unassigned_filter: bool = unassigned_raw.lower() in ('true', '1', 'yes')

    try:
        # PR #146: route reads through TaskRepository. The `unassigned`
        # branch is the only one without a direct repo method
        # (`list_by_agent` takes an agent_id, not "no agent"), so it
        # filters the full listing in Python — fine for the listing
        # cardinality this endpoint serves (≤ thousands).
        from ...repositories import task_repo

        if unassigned_filter:
            tasks_data = [
                t for t in task_repo.list_all()
                if t.get("assigned_to") in (None, "")
            ]
        elif assigned_to_filter is not None:
            tasks_data = task_repo.list_by_agent(assigned_to_filter)
        else:
            tasks_data = task_repo.list_all()
        return JSONResponse(tasks_data)
    except Exception as e:
        logger.error(f"Error fetching all tasks: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse({"error": "Failed to fetch all tasks"}, status_code=500)


@router.api_route("", methods=["POST", "OPTIONS"])
async def create_task_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Create a new task. PR D: auth via require_operator_session.

    Body: {"task_title", "task_description", "priority"?,
           "assigned_to"?, "parent_task"?, "required_capabilities"?}
    Returns: {"success": true, "task_id": "...", "message": "..."}
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        raw_title = data.get('task_title')
        description = data.get('task_description', '')
        priority = data.get('priority', 'medium')
        assigned_to = data.get('assigned_to')  # nullable
        parent_task = data.get('parent_task')  # nullable
        # Event-coord PR-1: optional capability gate (list of free-text
        # labels, normalized to lowercase+stripped+deduped at write
        # time via the shared helper). Empty/missing => stored as NULL
        # ("anyone can claim", matches broadcast semantics).
        from ...utils.capability_normalization import normalize_capabilities

        # The repo (task_repo.create) handles json.dumps internally.
        _norm_caps = normalize_capabilities(data.get('required_capabilities'))

        # F004 (verify-all-v6 MUTATING #3): distinguish an absent field
        # from one whose content was stripped to empty by the JSON-input
        # sanitizer (utils/json_utils.py removes NULL/control bytes and
        # zero-width Unicode BEFORE the JSON parse — a body like
        # ``{"task_title":"\x00\x01"}`` arrives here as
        # ``{"task_title":""}``). Conflating the two emits
        # "task_title is required" for a title that *was* sent, pointing
        # the operator at the wrong remediation. Whitespace-only titles
        # are also rejected here (previously they slipped through as a
        # truthy string and created a task named "   ").
        if raw_title is None:
            return JSONResponse(
                {"error": "task_title is required"}, status_code=400
            )
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        if not title:
            return JSONResponse(
                {
                    "error": "task_title_empty_after_strip",
                    "message": (
                        "task_title contains only whitespace or "
                        "non-printable characters after sanitization"
                    ),
                },
                status_code=400,
            )

        requesting_admin_id = caller_identity(auth)
        task_id = f"task_{_uuid.uuid4().hex[:12]}"
        status = 'pending' if assigned_to else 'unassigned'

        conn = get_db_connection()
        cursor = conn.cursor()
        # PR 7 (Task flip): create flows through task_repo.create with
        # the caller's cursor so the wider audit-log INSERT stays in
        # the same transaction. The repo handles JSON serialisation
        # of list fields + the `required_capabilities` quirk.
        from ...repositories import task_repo as _task_repo
        _task_repo.create(
            {
                "task_id": task_id,
                "title": title,
                "description": description,
                "assigned_to": assigned_to,
                "created_by": requesting_admin_id,
                "status": status,
                "priority": priority,
                "parent_task": parent_task,
                "child_tasks": [],
                "depends_on_tasks": [],
                "notes": [],
                "required_capabilities": _norm_caps if _norm_caps else None,
            },
            connection=cursor,
        )
        log_agent_action_to_db(
            cursor, requesting_admin_id, "created_task",
            task_id=task_id, details={"title": title, "assigned_to": assigned_to},
        )

        # BL-2: maintain the parent's child_tasks back-reference mirror in
        # the same transaction so hierarchy reads + the delete cascade see
        # this child. update_fields(connection=) defers the parent's cache
        # write to post-commit (reconciled below).
        parent_mirror_updated = False
        if parent_task:
            cursor.execute(
                "SELECT child_tasks FROM tasks WHERE task_id = ?",
                (parent_task,),
            )
            parent_row = cursor.fetchone()
            if parent_row is not None:
                import json as _json
                children = _json.loads(parent_row["child_tasks"] or "[]")
                if task_id not in children:
                    children.append(task_id)
                    _task_repo.update_fields(
                        parent_task,
                        {"child_tasks": children},
                        connection=cursor,
                    )
                    parent_mirror_updated = True

        conn.commit()

        # BL-1: task_repo.create(connection=) defers the g.tasks cache
        # write + EventBus publish to the caller (see the create()
        # docstring — a subscriber must never observe an uncommitted /
        # rolled-back row). Reconcile now that the transaction committed:
        # without this, the row is absent from view_tasks (which reads
        # g.tasks) and no wait_for_events waiter wakes for an assigned
        # REST-created task.
        fresh = _task_repo.get_by_id(task_id)
        if fresh is not None:
            _task_repo.upsert_cache(fresh)
        if parent_mirror_updated:
            fresh_parent = _task_repo.get_by_id(parent_task)
            if fresh_parent is not None:
                _task_repo.upsert_cache(fresh_parent)

        from ...core.repositories import _event_bus_shim
        _event_bus_shim.publish(
            assigned_to or "*",
            "task.created",
            {
                "task_id": task_id,
                "status": status,
                "assigned_to": assigned_to,
            },
        )
        if assigned_to:
            # Wake the assignee's wait_for_events waiter so a REST-assigned
            # task is delivered without polling.
            try:
                from ...core import globals as _g
                _g.notify_agent_inbox(assigned_to)
            except Exception as notify_exc:  # pragma: no cover - defensive
                logger.warning(
                    "notify_agent_inbox(%s) raised after REST create_task: %s",
                    assigned_to, notify_exc,
                )

        return JSONResponse({
            "success": True,
            "task_id": task_id,
            "message": f"Task '{title}' created successfully",
        })

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error creating task: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see fetch-all-tasks note.
        return JSONResponse(
            {"error": "Failed to create task"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


@router.api_route("/{task_id}", methods=["DELETE", "OPTIONS"])
async def delete_task_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/tasks/<task_id> — admin deletes a task.

    Thin adapter over the ``delete_task`` MCP tool (Candidate C,
    2026-06-02 architecture review). Validation
    (``task_id`` required) and admin-only auth live in the tool's
    ``inputSchema`` + ``@requires("admin")``. Cascade safety (children
    / dependents) is handled by the tool impl. Wire-shape parity is
    pinned by tests/test_rest_mcp_tool_parity.py.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'DELETE':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Extract task_id from URL path (last segment).
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "task_id is required in URL"}, status_code=400)
    task_id = path_parts[-1]

    try:
        _ = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # ``force_delete=True`` matches the legacy REST behavior (the
    # direct-DB handler had no cascade safety check). The MCP tool's
    # default is False; passing True here preserves wire compatibility
    # — the dashboard never sent force_delete, and silently failing on
    # tasks with children would break existing flows.
    return await _dispatch_through_tool(
        "delete_task",
        {"task_id": task_id, "force_delete": True},
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
        success_message=f"Task '{task_id}' deleted successfully",
    )
