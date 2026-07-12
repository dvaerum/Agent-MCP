"""Memories resource router — ``/api/memories/...``.

Wave 8 PR 1 of prancy-napping-pie: the memory CRUD handlers
mechanically moved out of ``app/routes.py`` onto this router:
``create_memory``, ``update_memory``, ``delete_memory``.

Two adjacent URLs intentionally live elsewhere:
  * ``/api/context-data`` (GET memory list) is on the
    ``composition`` router because the URL doesn't match
    ``/api/memories``.
  * ``/api/create-sample-memories`` (POST demo data) is on the
    ``composition`` router for the same reason.

URL stability wins; a future PR can migrate those URLs to the
canonical ``/api/memories/...`` shape alongside dashboard updates.

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _build_route_principal
from ._wire_validation import require_str as _require_str
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...core.tool_result import (
    Ok,
    tool_result_error_message,
    tool_result_to_http,
)
from ...tools.registry import ToolInputValidationError, dispatch_tool_call
from ...utils.json_utils import get_sanitized_json_body
from ...utils.string_utils import (
    UNSAFE_KEY_ERROR,
    has_unsafe_unicode_for_identifier,
)


router = APIRouter(
    prefix="/api/memories",
    tags=["memories"],
)


# R9-F2 (pentest): all three handlers (CREATE / UPDATE / DELETE) now
# dispatch through the gated MCP project_context tools so the tool-layer
# authorization gates — the ``config_aoe_*`` sysadmin gate (R8-F1), the
# viewer-tier write guard (SEC1), the per-key creator-ownership matrix,
# and the critical-key / ``force_delete`` guard — are enforced on the
# REST surface too. Before this fix UPDATE + DELETE wrote the table
# ORM-DIRECT and bypassed every one of them.
#
# SEC round-9 (type-confusion 400-not-500): ``description`` is a TEXT
# column — a structured JSON type used to 500. The ``_require_str``
# wire-hygiene guard on ``description`` is kept LOCAL to the CREATE +
# UPDATE handlers (the MCP path validates the same via the tool's
# inputSchema). ``context_value`` is intentionally arbitrary JSON
# (``json.dumps``-serialised inside the tool), so it is NOT guarded here.
#
# arch-r4 #10: ``_require_str`` now lives once in ``._wire_validation``
# (imported above) — the round-9 "kept local, do NOT consolidate" scope
# boundary is settled.


@router.post("")
async def create_memory_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Create a new memory entry — thin adapter over ``create_project_context``.

    E3 (arch-deepening): the create choreography (INSERT + uniqueness
    guard, ``created_memory`` audit, the BL-R14-1 post-write wake set)
    lives ONCE in
    :func:`agent_mcp.tools.project_context_tools.create_project_context_tool_impl`.
    project_context is a SQLAlchemy table, so that tool is ORM-based
    (``SessionLocal``) like its ``update_``/``delete_`` siblings — NOT the
    raw-sqlite unit-of-work. Before E3 this handler was the sole
    implementation (hand-rolled INSERT + audit + wakes). It now keeps only
    the HTTP-wire concerns — body sanitization, the SEC-round-9
    type-confusion guards, and the unsafe-unicode key check — then
    dispatches and maps the ``ToolResult`` to the legacy response shape.
    Auth stays operator-only via ``require_operator_session``.
    """
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    context_key = data.get('context_key')
    context_value = data.get('context_value')
    description = data.get('description')

    if not context_key:
        return JSONResponse({"error": "context_key is required"}, status_code=400)

    # SEC round-9: a dict/list ``context_key`` binds into the WHERE / ORM
    # column and 500s (and slips past the unsafe-unicode check below,
    # which returns False for non-str). ``description`` is a TEXT column —
    # reject structured JSON up front. Wire-level input hygiene kept local
    # (the MCP path validates the same via the tool's inputSchema).
    _err = _require_str(context_key, "context_key")
    if _err is not None:
        return _err
    _err = _require_str(description, "description")
    if _err is not None:
        return _err

    # F005 verify-all-v6 MUTATING #3: reject keys containing Unicode
    # control / bidi-override / invisible characters. See
    # ``agent_mcp/utils/string_utils.py`` for the rationale — short
    # version: a key like ``config<U+202E>drowssap`` renders in the
    # dashboard as ``configpassword`` (the RTL override flips display
    # order) but stores/searches as the original, a real spoofing vector.
    if has_unsafe_unicode_for_identifier(context_key):
        return JSONResponse(UNSAFE_KEY_ERROR, status_code=400)

    # Operator-session Principal (a forwarding VIEWER gets a viewer-role
    # Principal the tool's capability gate denies — AC-R5-1). Mirrors the
    # task / agent thin adapters.
    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    try:
        result = await dispatch_tool_call(
            "create_project_context",
            {
                "context_key": context_key,
                "context_value": context_value,
                "description": description,
            },
            principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return the STATIC generic 500 body this route has always used.
        logger.error(f"Error dispatching create_project_context: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to create memory"}, status_code=500)

    if isinstance(result, Ok):
        return JSONResponse(
            {
                "success": True,
                "message": (
                    result.message
                    or f"Memory '{context_key}' created successfully"
                ),
            },
            status_code=200,
        )

    # Error variants: STATUS from the shared C-wave adapter; body kept in
    # the legacy ``{"error": ...}`` envelope the dashboard + tests pin.
    # arch-r4 #10: body wording now comes from the ONE shared
    # :func:`tool_result_error_message` mapper. ``Conflict``'s ``reason``
    # is already the exact legacy 409 wording ("Memory with this key
    # already exists" — set at the source in
    # ``create_project_context_tool_impl``). ``Failed`` (or any residual
    # variant, BL-R5-2 / SEC-R8-1) falls back to the same static "Failed
    # to create memory" this route has always used — no exception-detail
    # leak; the tool impl already logged the real detail server-side.
    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {"error": tool_result_error_message(result, "Failed to create memory")},
        status_code=status,
    )


@router.put("/{context_key}")
async def update_memory_api_route(
    context_key: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Update a memory entry — thin adapter over ``update_project_context``.

    R9-F2 (pentest): this handler used to write the ``project_context``
    table ORM-DIRECT after the ``require_operator_session`` gate, which
    BYPASSED every authorization gate that lives inside the MCP tool
    impl — the ``config_aoe_*`` sysadmin gate (R8-F1), the viewer-tier
    write guard (SEC1), and the per-key creator-ownership matrix. A
    non-sysadmin per-project operator could therefore re-point the
    outbound AoE client via ``config_aoe_base_url`` (SSRF; the R8-F1
    vuln reopened via UPDATE) even though the POST create surface
    already 403s it. The write now dispatches through the gated
    ``update_project_context`` tool exactly as the CREATE handler above
    dispatches ``create_project_context`` — ONE enforcement path. The
    partial-description-preservation (BL-R22-1) + the BL-R14-1 post-write
    wake set now live entirely in the tool, so this handler keeps only
    the HTTP-wire concerns.

    arch-r4 #10: ``context_key`` is a typed path parameter.
    """
    # F005 verify-all-v6 MUTATING #3: reject keys with Unicode
    # control / bidi-override / invisible chars. Matches the
    # CREATE-handler check above so update can't backdoor a
    # spoofing-prone key (and so PUT to a URL-encoded unsafe key
    # returns 400 — the actionable rejection — rather than 404
    # "not found", which leaks the same information after the
    # decoder has already let the unsafe payload in).
    if has_unsafe_unicode_for_identifier(context_key):
        return JSONResponse(UNSAFE_KEY_ERROR, status_code=400)

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    context_value = data.get('context_value')
    description = data.get('description')

    # SEC round-9: ``description`` binds into the TEXT column — reject
    # structured JSON up front (wire-level hygiene kept local; the MCP
    # path validates the same via the tool's inputSchema). ``context_value``
    # is arbitrary JSON so it stays unguarded, matching the CREATE handler.
    _err = _require_str(description, "description")
    if _err is not None:
        return _err

    # Operator-session Principal (a forwarding VIEWER gets a viewer-role
    # Principal the tool's capability gate denies — SEC1). Mirrors the
    # CREATE handler above.
    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    # Only thread ``description`` when the caller supplied it so the tool's
    # partial-update semantics preserve an existing description (BL-R22-1).
    arguments: dict = {"context_key": context_key, "context_value": context_value}
    if description is not None:
        arguments["description"] = description

    try:
        result = await dispatch_tool_call(
            "update_project_context",
            arguments,
            principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return the STATIC generic 500 body this route has always used.
        logger.error(f"Error dispatching update_project_context: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to update memory"}, status_code=500)

    if isinstance(result, Ok):
        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' updated successfully",
        })

    # Error variants: STATUS from the shared adapter; body kept in the
    # legacy ``{"error": ...}`` envelope. ``Failed`` falls back to the
    # static "Failed to update memory" this route has always used — the
    # tool impl already logged the real detail server-side (BL-R5-2 / no
    # exception-detail leak).
    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {"error": tool_result_error_message(result, "Failed to update memory")},
        status_code=status,
    )


@router.delete("/{context_key}")
async def delete_memory_api_route(
    context_key: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/memories/<context_key> — thin adapter over
    ``delete_project_context``.

    R9-F2 (pentest): this route wrote the ``project_context`` table
    ORM-DIRECT (its prior docstring even noted it "never enforced the
    critical-keys guard"), so — like the UPDATE sibling — it BYPASSED
    every gate inside the MCP tool: the ``config_aoe_*`` sysadmin gate
    (R8-F1; a non-sysadmin operator could delete ``config_aoe_bearer_token``),
    the viewer-tier write guard (SEC1), the per-key creator-ownership
    matrix, AND the critical-key / ``force_delete`` guard. It now
    dispatches through the gated ``delete_project_context`` tool exactly
    as the CREATE handler dispatches ``create_project_context`` — ONE
    enforcement path. The RAG chunk-purge (BL-R5-1) and the post-delete
    wake set (R1-F3 / BL-R14-1) live entirely in the tool now.

    ``force_delete`` is read from the JSON body (default ``False``) and
    threaded to the tool, so the critical-key guard is ENFORCED on this
    surface: deleting a critical system key (any ``config_*`` key, plus
    ``server_*`` / ``database_version`` / ``system_config`` /
    ``mcp_server_url``) now requires an explicit ``force_delete: true``
    — it no longer silently succeeds. The MCP tool keeps its own gates
    for non-REST callers — untouched.

    Auth: the outer ``require_operator_session`` dep accepts cookie /
    signed forwarding-header / operator-tier bearer. arch-r4 #10:
    ``context_key`` is a typed path parameter.
    """
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    force_delete = bool(data.get("force_delete", False)) if isinstance(data, dict) else False

    # Operator-session Principal (a forwarding VIEWER gets a viewer-role
    # Principal the tool's capability gate denies — SEC1). Mirrors the
    # CREATE / UPDATE handlers.
    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    try:
        result = await dispatch_tool_call(
            "delete_project_context",
            {"context_key": context_key, "force_delete": force_delete},
            principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return the STATIC generic 500 body this route has always used.
        logger.error(f"Error dispatching delete_project_context: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to delete memory"}, status_code=500)

    if isinstance(result, Ok):
        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' deleted successfully",
        })

    # Error variants: STATUS from the shared adapter; body kept in the
    # legacy ``{"error": ...}`` envelope. ``NotFound`` keeps the legacy
    # "Memory '<key>' not found" wording via ``not_found_label``; ``Failed``
    # falls back to the static "Failed to delete memory" (BL-R5-2 — no
    # exception-detail leak; the tool impl already logged the real detail).
    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {
            "error": tool_result_error_message(
                result, "Failed to delete memory", not_found_label="Memory"
            )
        },
        status_code=status,
    )
