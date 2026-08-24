"""Schedules resource router — ``/api/schedules/...`` (event-loop scheduler).

The operator/admin REST surface behind the dashboard **Schedules** page
(plan §5.5). All handlers are ``require_operator_session``-gated
(sysadmin ⊇ operator).

One enforcement path: the create / update / delete handlers reuse the SAME
MCP tool impls the agent surface uses
(``agent_mcp.tools.scheduled_directive_tools``) with an operator-tier
Principal, so the guardrails (min-interval floor, max active per agent) and
validation run identically regardless of surface — mirrors the messages
router reusing ``check_send_message_permission``. The list handler is a
plain read over the repository (every schedule across the project).

The per-agent **poke** button posts to the existing
``POST /api/agents/{id}/directive`` route (agents router) — not duplicated
here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _build_route_principal
from ..deps import require_operator_session
from ..rest_principal import RestPrincipal
from ...core.authorize import AuthRejected
from ...core.config import logger
from ...core.tool_result import Ok, tool_result_to_http
from ...db.connection import get_db_connection
from ...repositories import scheduled_directive_repository as _repo
from ...tools.scheduled_directive_tools import (
    _serialize,
    create_scheduled_directive_tool_impl,
    delete_scheduled_directive_tool_impl,
    update_scheduled_directive_tool_impl,
)
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(prefix="/api/schedules", tags=["schedules"])


def _operator_principal(auth: RestPrincipal):
    return _build_route_principal(auth=auth)


def _tool_result_to_response(result) -> JSONResponse:
    """Map a scheduled-directive ToolResult to the dashboard's JSON shape.

    ``Ok`` → 200 with the tool's ``data`` (``{directive: {...}}`` or
    ``{deleted: id}``); anything else → the shared ``tool_result_to_http``
    status + ``{error: <message>}`` body.
    """
    if isinstance(result, Ok):
        return JSONResponse({"success": True, **(result.data or {})})
    status, body = tool_result_to_http(result)
    return JSONResponse(
        {"error": body.get("message", "Request rejected")},
        status_code=status,
    )


@router.get("")
async def list_schedules_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/schedules — every schedule across the project's agents.

    Returns ``{schedules: [...]}`` grouped agent-then-soonest-due (the
    repository's ordering), each row the public ``_serialize`` shape the
    MCP tools return, so the dashboard renders a uniform table.
    """
    conn = None
    try:
        conn = get_db_connection()
        rows = _repo.list_all(connection=conn.cursor())
    except Exception as e:
        logger.error("list_schedules failed: %s", e, exc_info=True)
        return JSONResponse(
            {"error": "Failed to list schedules"}, status_code=500
        )
    finally:
        if conn:
            conn.close()
    return JSONResponse({"schedules": [_serialize(r) for r in rows]})


@router.post("")
async def create_schedule_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/schedules — operator creates a schedule for any agent.

    Body: ``{agent_id, prompt, interval_seconds, until?, count?, run_now?}``.
    """
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        result = await create_scheduled_directive_tool_impl(
            data, principal=_operator_principal(auth),
        )
    except AuthRejected as e:
        # AC-R5-1 / R21-F1 class: a tool's @requires_* gate RAISES, so
        # without this arm a routine denial (e.g. a forwarding VIEWER that
        # passes require_operator_session but lacks the tool's cap) lands
        # in the generic 500 below. Rationale + sweep:
        # tests/test_arch_enforced_authrejected_403.py.
        return JSONResponse({"error": e.reason}, status_code=403)
    except Exception as e:
        # Defense-in-depth (R16-F3): these handlers call the tool impl
        # DIRECTLY (bypassing dispatch_tool_call's envelope), so an impl
        # exception would otherwise leak as FastAPI's raw text/plain 500.
        # Mirror the messages/tasks/memories routers' generic 500 shape.
        logger.error("create_schedule failed: %s", e, exc_info=True)
        return JSONResponse(
            {"error": "Failed to create schedule"}, status_code=500
        )
    return _tool_result_to_response(result)


@router.put("/{directive_id}")
async def update_schedule_api_route(
    directive_id: str,
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """PUT /api/schedules/{id} — edit / pause / resume.

    Body: ``{prompt?, interval_seconds?, enabled?, until?, count?}``. The
    inline enable/disable toggle sends ``{enabled: bool}``.
    """
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    args = {**data, "directive_id": directive_id}
    try:
        result = await update_scheduled_directive_tool_impl(
            args, principal=_operator_principal(auth),
        )
    except AuthRejected as e:
        # AC-R5-1 / R21-F1 class: a tool's @requires_* gate RAISES, so
        # without this arm a routine denial (e.g. a forwarding VIEWER that
        # passes require_operator_session but lacks the tool's cap) lands
        # in the generic 500 below. Rationale + sweep:
        # tests/test_arch_enforced_authrejected_403.py.
        return JSONResponse({"error": e.reason}, status_code=403)
    except Exception as e:
        # Defense-in-depth (R16-F3) — see create handler above.
        logger.error("update_schedule failed: %s", e, exc_info=True)
        return JSONResponse(
            {"error": "Failed to update schedule"}, status_code=500
        )
    return _tool_result_to_response(result)


@router.delete("/{directive_id}")
async def delete_schedule_api_route(
    directive_id: str,
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/schedules/{id} — remove a schedule permanently."""
    try:
        result = await delete_scheduled_directive_tool_impl(
            {"directive_id": directive_id},
            principal=_operator_principal(auth),
        )
    except AuthRejected as e:
        # AC-R5-1 / R21-F1 class: a tool's @requires_* gate RAISES, so
        # without this arm a routine denial (e.g. a forwarding VIEWER that
        # passes require_operator_session but lacks the tool's cap) lands
        # in the generic 500 below. Rationale + sweep:
        # tests/test_arch_enforced_authrejected_403.py.
        return JSONResponse({"error": e.reason}, status_code=403)
    except Exception as e:
        # Defense-in-depth (R16-F3) — see create handler above.
        logger.error("delete_schedule failed: %s", e, exc_info=True)
        return JSONResponse(
            {"error": "Failed to delete schedule"}, status_code=500
        )
    return _tool_result_to_response(result)
