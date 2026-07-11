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

import datetime
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _build_route_principal, handle_options
from ._wire_validation import require_str as _require_str
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...core.tool_result import (
    Ok,
    tool_result_error_message,
    tool_result_to_http,
)
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.engine import SessionLocal
from ...db.models import ProjectContext
from ...tools.project_context_tools import emit_context_write_wakes
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


# SEC round-9 (type-confusion 400-not-500): these direct-SQL handlers
# bypass the schema-validating MCP tool dispatch. ``context_key`` binds
# straight into a WHERE clause / the ORM column, and ``description`` is
# a TEXT column — a structured JSON type in either used to 500 (or, for
# a non-str ``context_key``, slip past ``has_unsafe_unicode_for_identifier``
# which returns False for non-str). ``context_value`` is intentionally
# arbitrary JSON (``json.dumps``-serialised), so it is NOT guarded here.
#
# arch-r4 #10: ``_require_str`` now lives once in ``._wire_validation``
# (imported above) — the round-9 "kept local, do NOT consolidate" scope
# boundary is settled.


@router.api_route("", methods=["POST", "OPTIONS"])
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
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

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


@router.api_route("/{context_key}", methods=["PUT", "OPTIONS"])
async def update_memory_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Update an existing memory entry. PR D: auth via require_operator_session."""
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'PUT':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Extract context_key from URL path
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "context_key is required in URL"}, status_code=400)

    context_key = path_parts[-1]

    # F005 verify-all-v6 MUTATING #3: reject keys with Unicode
    # control / bidi-override / invisible chars. Matches the
    # CREATE-handler check above so update can't backdoor a
    # spoofing-prone key (and so PUT to a URL-encoded unsafe key
    # returns 400 — the actionable rejection — rather than 404
    # "not found", which leaks the same information after the
    # decoder has already let the unsafe payload in).
    if has_unsafe_unicode_for_identifier(context_key):
        return JSONResponse(UNSAFE_KEY_ERROR, status_code=400)

    session = None
    try:
        data = await get_sanitized_json_body(request)
        context_value = data.get('context_value')
        description = data.get('description')

        # SEC round-9: ``description`` binds into the TEXT column — reject
        # structured JSON. ``context_value`` is arbitrary JSON (json.dumps
        # -serialised) so it stays unguarded, matching the CREATE handler.
        _err = _require_str(description, "description")
        if _err is not None:
            return _err

        requesting_admin_id = caller_identity(auth)

        session = SessionLocal()

        # Check if memory exists
        row = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == context_key)
            .one_or_none()
        )
        if row is None:
            return JSONResponse({"error": "Memory not found"}, status_code=404)

        current_time = datetime.datetime.now().isoformat()

        # Apply partial-update semantics: only overwrite the fields the
        # caller actually supplied. Matches the legacy raw-SQL behavior.
        # created_at / created_by stay untouched on an UPDATE.
        row.updated_at = current_time
        row.updated_by = requesting_admin_id
        if context_value is not None:
            row.value = json.dumps(context_value)
        if description is not None:
            row.description = description

        session.flush()

        # Log the action
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(cursor, requesting_admin_id, "updated_memory", details={"context_key": context_key})
        session.commit()

        # BL-R14-1: fire the full post-write wake set this key requires
        # (loop toggle → wake_all_for_flag_recheck; worker-capability
        # toggle → tools/list_changed). Shared with the MCP write
        # surface so both fire the SAME wakes. See the create handler.
        await emit_context_write_wakes(context_key)

        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' updated successfully"
        })

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if session is not None:
            session.rollback()
        logger.error(f"Error updating memory: {e}", exc_info=True)
        # BL-R5-2: generic message — ``str(e)`` on a SQLAlchemyError
        # embeds SQL text + bound params (schema disclosure).
        return JSONResponse({"error": "Failed to update memory"}, status_code=500)
    finally:
        if session is not None:
            session.close()


@router.api_route("/{context_key}", methods=["DELETE", "OPTIONS"])
async def delete_memory_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/memories/<context_key> — operator deletes a memory.

    Writes the DB directly via SQLAlchemy, mirroring the sibling
    CREATE (``create_memory_api_route``) and UPDATE
    (``update_memory_api_route``) handlers above. Auth: the outer
    ``require_operator_session`` dep accepts cookie / signed
    forwarding-header / operator-tier bearer; if we reached the
    handler the caller is authorised.

    F005 (verify-all-v4) fix — 2026-06-25
    -----------------------------------------
    This route previously dispatched through the
    ``delete_project_context`` MCP tool via ``_dispatch_through_tool``.
    That tool is gated by ``@requires("any")``, whose ``_check_role``
    branch intentionally rejects operator-session callers that don't
    carry a per-agent token (audit attribution needs an
    ``agent_id``; see ``agent_mcp/core/authorize.py:120-125``).
    The dashboard's DELETE body is ``{}`` (no token field; cf.
    ``agent_mcp/dashboard/lib/api.ts:844-849``) and the cookie path
    intentionally doesn't synthesise a god-key bearer post
    retire-system-token Wave 1 — so the dispatch returned
    ``Unauthorized: Valid token required`` (403) for every
    cookie-authenticated operator.

    The CREATE and UPDATE siblings never had this regression because
    they write the project_context table directly after the
    ``require_operator_session`` gate; this handler now follows the
    same shape. The MCP tool keeps its own ``@requires("any")`` guard
    for non-REST callers — untouched. ``force_delete=true`` semantics
    are preserved by virtue of the REST layer never enforcing the
    critical-keys guard (it never did; the comment that lived here
    before explained that we passed ``force_delete=true`` to the tool
    to preserve that legacy behavior). Wire-shape parity is pinned by
    ``tests/test_rest_mcp_tool_parity.py``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'DELETE':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Extract context_key from URL path
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "context_key is required in URL"}, status_code=400)

    context_key = path_parts[-1]

    # Consume the JSON body if present (validates that it parses, and
    # — historically — gave the dep the body-token. We no longer act
    # on it here; the dep has already authorised the caller.)
    try:
        await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    requesting_admin_id = caller_identity(auth)

    session = None
    try:
        session = SessionLocal()

        row = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == context_key)
            .one_or_none()
        )
        if row is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Memory '{context_key}' not found",
                    "message": f"Memory '{context_key}' not found",
                },
                status_code=404,
            )

        session.delete(row)
        session.flush()

        # Audit attribution. Same shape as CREATE/UPDATE — log through
        # the session's raw connection so it lands in the same
        # transaction as the row delete.
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(
            cursor,
            requesting_admin_id,
            "deleted_memory",
            details={"context_key": context_key},
        )

        # BL-R5-1: prune the deleted memory's RAG chunk + hash watermark
        # in the SAME transaction as the row delete, mirroring the MCP
        # ``delete_project_context`` tool (project_context_tools.py). The
        # REST surface (the dashboard's primary delete path) previously
        # skipped this, so a ``source_type='context'`` chunk for the
        # deleted key survived and stayed retrievable via
        # ``ask_project_rag`` forever — the incremental indexer keys on
        # ``updated_at`` and never sweeps orphans. Purging on the shared
        # cursor means a purge failure rolls back the row delete too.
        from ...repositories import rag_repo

        rag_repo.purge_source("context", context_key, connection=cursor)

        session.commit()

        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' deleted successfully",
        })

    except Exception as e:
        if session is not None:
            session.rollback()
        logger.error(f"Error deleting memory: {e}", exc_info=True)
        # BL-R5-2: return a generic message — ``str(e)`` on a
        # SQLAlchemyError embeds the SQL text + bound parameters
        # (schema disclosure). The details are in the server log above.
        return JSONResponse(
            {"error": "Failed to delete memory"},
            status_code=500,
        )
    finally:
        if session is not None:
            session.close()
