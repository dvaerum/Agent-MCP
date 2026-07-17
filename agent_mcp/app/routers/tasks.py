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

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import (
    _build_route_principal,
    _dispatch_through_tool,
)
from ._read_limits import _clamp_section_limit
from ._wire_validation import require_str as _require_str
from ._wire_validation import require_str_list as _require_str_list
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...core.tool_result import (
    Ok,
    tool_result_error_message,
    tool_result_to_http,
)
from ...tools.registry import ToolInputValidationError, dispatch_tool_call
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(
    prefix="/api/tasks",
    tags=["tasks"],
)


# SEC round-9 (type-confusion 400-not-500): these direct-SQL handlers
# bypass the schema-validating MCP tool dispatch, so a structured JSON
# type (dict / list) in a string-typed field reaches a SQLite bind and
# surfaces as an uncaught 500 — or is silently stored as bad data.
# Guard every user-supplied field up front.
#
# arch-r4 #10: ``_require_str`` / ``_require_str_list`` now live once in
# ``._wire_validation`` (imported above) — the round-9 "kept local, do
# NOT consolidate" scope boundary is settled.


@router.get("")
async def all_tasks_api_route(request: Request) -> JSONResponse:
    # GET /api/tasks[?assigned_to=<agent_id>][?unassigned=true]
    #     [?assigned=true][?status=<concrete|incomplete|active|open>]
    #     [?created_by=<agent_id>]
    #
    # Operator-gated (the dashboard operator sees ALL tasks) — there is
    # no worker-visibility gate here; every param NARROWS the operator's
    # full list. All params are optional and AND-combined.
    #
    # Default (no query params): returns every task row (back-compat
    # with the existing dashboard listing).
    #
    # `?assigned_to=<agent_id>` filters to tasks whose assigned_to
    # column matches exactly. Replaces the router's `list_tasks_for`
    # synthetic.
    #
    # `?unassigned=true` filters to tasks with assigned_to IS NULL/empty.
    # Replaces the router's `list_unassigned_tasks` synthetic.
    #
    # `?assigned=true` is the complement — only tasks that HAVE an
    # assignee (assigned_to IS NOT NULL/empty). Parsed the same way as
    # `unassigned`. `assigned=true` + `unassigned=true` is contradictory
    # (AND) → matches nothing.
    #
    # `?status=<value>` filters via the shared
    # ``status_filter_matches`` helper (single source with the MCP
    # view_tasks / search_tasks tools): a concrete status
    # (pending/in_progress/completed/cancelled/failed) is exact-match;
    # the pseudo-values incomplete/active/open expand to any non-terminal
    # status (pending + in_progress).
    #
    # `?created_by=<agent_id>` filters to tasks whose created_by matches
    # exactly.
    #
    # Phase 7c, Q7.2 in plan.
    assigned_to_filter: Optional[str] = request.query_params.get('assigned_to')
    unassigned_raw = request.query_params.get('unassigned', '')
    unassigned_filter: bool = unassigned_raw.lower() in ('true', '1', 'yes')
    assigned_raw = request.query_params.get('assigned', '')
    assigned_filter: bool = assigned_raw.lower() in ('true', '1', 'yes')
    status_filter: Optional[str] = request.query_params.get('status')
    created_by_filter: Optional[str] = request.query_params.get('created_by')

    # pentest R3-F3: bound this list read. Before this the default (no
    # filter) branch did an unbounded ``SELECT … FROM tasks`` — a project
    # with thousands of tasks materialised the whole table on every call.
    # Reuse the SAME ``?limit`` clamp the composition ``*-data`` reads use
    # (single source of truth in ``._read_limits``): honored, clamped to
    # ``[1, _ALL_DATA_MAX_LIMIT]``, defaulting to ``_ALL_DATA_DEFAULT_LIMIT``,
    # applied as ``ORDER BY created_at DESC LIMIT ?`` in SQL. A small corpus
    # returns in full; the response shape (a JSON array of task dicts) is
    # unchanged.
    limit = _clamp_section_limit(request)

    try:
        # PR #146: route reads through TaskRepository. The candidate read
        # is bounded in SQL FIRST (the ``?limit`` clamp), then the
        # discovery filters (unassigned / assigned / created_by / status)
        # are AND-combined in Python over that bounded listing — so we
        # read at most ``limit`` rows (a bounded superset of any filtered
        # subset, which is the intended DoS bound, not an unbounded scan).
        # ``status_filter_matches`` is imported from the shared
        # ``features.task_queries`` helper so this REST surface interprets
        # the incomplete/active/open pseudo-values identically to the MCP
        # view_tasks / search_tasks tools (single source of truth).
        from ...repositories import task_repo
        from ...features.task_queries import status_filter_matches

        if assigned_to_filter is not None:
            candidates = task_repo.list_by_agent(assigned_to_filter, limit=limit)
        else:
            candidates = task_repo.list_all(limit=limit)

        def _keep(task: dict) -> bool:
            assignee = task.get("assigned_to")
            if unassigned_filter and assignee not in (None, ""):
                return False
            if assigned_filter and assignee in (None, ""):
                return False
            if (
                created_by_filter is not None
                and task.get("created_by") != created_by_filter
            ):
                return False
            if status_filter is not None and not status_filter_matches(
                status_filter, task.get("status")
            ):
                return False
            return True

        tasks_data = [t for t in candidates if _keep(t)]
        return JSONResponse(tasks_data)
    except Exception as e:
        logger.error(f"Error fetching all tasks: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse({"error": "Failed to fetch all tasks"}, status_code=500)


@router.post("")
async def create_task_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Create a new task — thin adapter over the ``create_task`` MCP tool.

    E1 (arch-deepening): the create choreography (assignability +
    capability gates, ``task_repo.create``, ``current_task`` reconcile,
    parent ``child_tasks`` mirror, ``created_task`` audit, cache upsert,
    ``task.created`` publish, assignee / unassigned wake) lives ONCE in
    :func:`agent_mcp.tools.task_tools.create_task_tool_impl` on the
    unit-of-work. Before E1 this handler hand-reimplemented all of it and
    imported ``_``-prefixed tool internals to stay in parity (the
    BL-R13-1 / AZ-R26-1 / BL-R15-1 ledger). Now it keeps only the
    HTTP-wire concerns — body sanitization, the SEC-round-9
    type-confusion guards, and the missing-vs-empty title distinction —
    then dispatches and maps the ``ToolResult`` to the legacy response
    shape. Auth stays operator-only via ``require_operator_session``.

    Body: {"task_title", "task_description", "priority"?,
           "assigned_to"?, "parent_task"?, "required_capabilities"?}
    Returns: {"success": true, "task_id": "...", "message": "..."}
    """
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    raw_title = data.get('task_title')
    description = data.get('task_description', '')
    priority = data.get('priority', 'medium')
    assigned_to = data.get('assigned_to')  # nullable
    parent_task = data.get('parent_task')  # nullable

    # SEC round-9: reject structured JSON in string/list fields BEFORE
    # dispatch. (``task_title`` is handled by the isinstance-guarded strip
    # below.) Wire-level input hygiene, kept local per the round-9 scope —
    # the MCP path validates the same via the tool's inputSchema.
    for _val, _name in (
        (description, "task_description"),
        (priority, "priority"),
        (assigned_to, "assigned_to"),
        (parent_task, "parent_task"),
    ):
        _err = _require_str(_val, _name)
        if _err is not None:
            return _err
    _caps_err = _require_str_list(
        data.get('required_capabilities'), "required_capabilities"
    )
    if _caps_err is not None:
        return _caps_err

    # F004 (verify-all-v6 MUTATING #3): distinguish an absent title from
    # one whose content was stripped to empty by the JSON-input sanitizer
    # (utils/json_utils.py removes NULL/control bytes and zero-width
    # Unicode BEFORE the JSON parse — a body like ``{"task_title":"\x00"}``
    # arrives here as ``{"task_title":""}``). This distinction depends on
    # the raw HTTP body + sanitizer, so it stays a wire-level concern here
    # rather than in the tool. Whitespace-only titles are also rejected.
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

    arguments = {
        "task_title": title,
        "task_description": description,
        "priority": priority,
        "assigned_to": assigned_to,
        "parent_task": parent_task,
        "required_capabilities": data.get('required_capabilities'),
    }

    # Operator-session Principal (forwarding VIEWER gets a viewer-role
    # Principal the tool's capability gate denies — AC-R5-1). Mirrors
    # ``delete_task_api_route`` / the other thin adapters.
    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    try:
        result = await dispatch_tool_call(
            "create_task", arguments, principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return the STATIC generic 500 body this route has always used.
        logger.error(f"Error dispatching create_task: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Failed to create task"}, status_code=500
        )

    if isinstance(result, Ok):
        return JSONResponse(
            {
                "success": True,
                "task_id": result.data["task_id"],
                "message": (
                    result.message
                    or f"Task '{title}' created successfully"
                ),
            },
            status_code=200,
        )

    # Error variants: STATUS from the shared C-wave adapter; body kept in
    # the legacy ``{"error": ...}`` envelope the dashboard + tests pin.
    # arch-r4 #10: body wording now comes from the ONE shared
    # :func:`tool_result_error_message` mapper. ``not_found_label="Parent
    # task"`` preserves this route's historical wording (the only
    # ``NotFound`` ``create_task`` can return is the missing parent).
    # ``Failed`` (or any residual variant, SEC-R6 / SD-R6-1) falls back to
    # the same static "Failed to create task" this route has always used
    # — no exception-detail leak; the dispatcher already logged the real
    # one.
    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {
            "error": tool_result_error_message(
                result, "Failed to create task",
                not_found_label="Parent task",
            )
        },
        status_code=status,
    )


@router.delete("/{task_id}")
async def delete_task_api_route(
    task_id: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/tasks/<task_id> — admin deletes a task.

    Thin adapter over the ``delete_task`` MCP tool (Candidate C,
    2026-06-02 architecture review). Admin-only auth lives in the
    tool's ``@requires("admin")``. Cascade safety (children /
    dependents) is handled by the tool impl. Wire-shape parity is
    pinned by tests/test_rest_mcp_tool_parity.py.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    arch-r4 #10: ``task_id`` is now a typed path parameter — FastAPI's
    routing already guarantees a non-empty value (the ``str`` convertor
    requires 1+ chars), so ``DELETE /api/tasks/`` (empty id) now 404s
    at the framework level instead of reaching a hand-written 400. The
    only test that touches this
    (``test_delete_task_missing_task_id_returns_400``) accepts
    400/404/405 for exactly this reason — still green.
    """
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
