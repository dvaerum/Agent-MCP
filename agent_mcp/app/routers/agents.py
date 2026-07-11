"""Agents resource router — ``/api/agents/...``.

Wave 8 PR 1 of prancy-napping-pie: the agent handlers mechanically
moved out of ``app/routes.py`` onto this router:
``agents_list``, ``register_agent_dashboard``, ``restore_agent``,
``edit_agent``, ``purge_preview``, ``purge_agent``.

The ``terminate_agent_dashboard`` handler lives on the
``composition`` router (not on this router) because its URL is
``/api/terminate-agent`` — it doesn't match the ``/api/agents``
prefix. URL stability wins; a future PR can migrate the URL to
``/api/agents/terminate`` alongside dashboard updates.

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR — several listing endpoints
(``GET /api/agents``, ``GET /api/agents/{id}/purge-preview``) are
currently open today and hoisting the gate to the router would
silently flip their auth behavior, which is out of scope for a
mechanical URL-stable move.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _build_route_principal
from ._wire_validation import require_str as _require_str
from ._wire_validation import require_str_list as _require_str_list
from ..deps import (
    caller_identity,
    forwarding_route_role,
    require_operator_session,
)
from ...core.config import logger
from ...core import session_registry
from ...core.principal_builder import build_operator_principal
from ...core.tool_result import (
    Failed as _Failed,
    Ok as _Ok,
    ToolResult,
    tool_result_error_message,
    tool_result_to_http,
)
from ...db.connection import get_db_connection
from ...tools.admin_tools import (
    _gather_purge_preview,
    _purge_tombstone,
    register_agent_tool_impl,
)
from ...tools.registry import ToolInputValidationError, dispatch_tool_call
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
)


# SEC round-9 (type-confusion 400-not-500): the edit-agent handler writes
# the DB directly, bypassing the schema-validating MCP tool dispatch.
# ``color`` / ``working_directory`` bind straight into TEXT columns and
# 500 on a dict/list; ``capabilities`` feeds ``normalize_capabilities``
# which iterates a dict's KEYS (or a str's CHARS) and silently stores bad
# data — and that data lands in ``g.active_agents`` which the task-claim
# authz gate consumes. Guard every editable field up front.
#
# arch-r4 #10: ``_require_str`` / ``_require_str_list`` now live once in
# ``._wire_validation`` (imported above) — the round-9 "kept local, do
# NOT consolidate" scope boundary is settled. ``capabilities`` here has
# always rejected an explicit ``None`` (unlike ``create_task``'s
# ``required_capabilities``, which treats ``None`` as "not supplied"),
# so the call site below passes ``allow_none=False``.


def _mcp_presence_for(agent_id: str) -> Dict[str, Any]:
    """Return ``{"online": bool, "last_mcp_connection": str | None}``
    for ``agent_id`` derived from :mod:`agent_mcp.core.session_registry`.

    Wave 7 PR 2 — coordinator transition. The dashboard's agents list
    no longer surfaces spawn metadata ("tmux session active") — the
    register-only flow doesn't spawn anything. Presence is now a live
    signal: ``online`` iff this agent has at least one live MCP stream
    subscribed, ``last_mcp_connection`` reflects the most recent
    ``last_seen_at`` across that agent's sessions (or ``None`` if the
    agent has never opened a stream since the backend booted).

    Returns the most-recent-handle's ``last_seen_at`` because session
    rows have no explicit ``ever_connected`` flag — a row exists iff a
    stream has been opened at some point in this process's lifetime,
    so "any handle present" is the strongest signal we have. When no
    handles exist, ``last_mcp_connection`` is ``None`` and the
    dashboard renders the "Pending — paste snippet" state.

    Wave 8 PR 1: the function lives here (agents router module)
    because the agents-list endpoint is its primary caller; the
    ``/api/all-data`` composition route imports it from here too.
    """
    try:
        handles = session_registry.sessions_for_agent(agent_id)
    except Exception:
        # Defensive: a DB hiccup here must not 500 the agents list;
        # treat it as "no presence data" so the UI degrades to the
        # legacy status pill instead of erroring out the page.
        logger.exception(
            "session_registry lookup failed for agent_id=%r", agent_id,
        )
        return {"online": False, "last_mcp_connection": None}
    if not handles:
        return {"online": False, "last_mcp_connection": None}
    # ``last_seen_at`` is the ISO-UTC timestamp the transport bumps
    # on every heartbeat; the max across handles is the most recent
    # liveness signal. "Online" means at least one runtime queue is
    # currently attached for one of the agent's handles — i.e. the
    # transport layer believes the SSE writer is still draining
    # payloads. Without a runtime queue the row is stale (the backend
    # restarted, the client hasn't reconnected yet).
    last_seen = max(h.last_seen_at for h in handles)
    online = any(
        session_registry.get_runtime_queue(h.session_id) is not None
        for h in handles
    )
    return {"online": online, "last_mcp_connection": last_seen}


@router.get("")
async def agents_list_api_route(request: Request) -> JSONResponse:
    # GET /api/agents[?status=<status>]
    #
    # Returns every non-tombstone agent row. Tombstone rows
    # (status='tombstone', agent_id like '[deleted-<original>]') are
    # FK-target artefacts of the purge cascade and never belong in
    # user-facing output — see all_data_api_route for the same filter
    # rationale.
    #
    # Wave 4 (cleanup/wave-4-delete-admin-pseudo-agent): the
    # hardcoded synthetic ``{'agent_id': 'Admin', 'status': 'system'}``
    # entry previously prepended here is gone. The underlying admin
    # pseudo-agent row has been deleted from the ``agents`` table
    # (migration 0014); rendering a UI-only stand-in for it would
    # contradict the retirement. Out-of-tree consumers that depended
    # on the synthesised row should stop relying on it.
    #
    # With `status=<value>`, returns only agent rows whose status
    # matches exactly, EXCEPT `status=tombstone` which always
    # returns the empty list (tombstone is an internal DB state,
    # not an operator-queryable agent status). This shape replaces
    # the router's `list_agents` synthetic tool (Phase 7c, Q7.2 in
    # plan).
    status_filter: Optional[str] = request.query_params.get('status')
    agents_list_data: List[Dict[str, Any]] = []
    conn = None
    try:
        # tombstone rows are a DB-internal FK artefact; refuse the
        # query early so a curious operator can't read them out.
        if status_filter == 'tombstone':
            return JSONResponse([])

        conn = get_db_connection()
        cursor = conn.cursor()
        if status_filter is None:
            # WHERE status != 'tombstone' filters the cascade-tombstone
            # rows out at the DB layer.
            cursor.execute(
                "SELECT agent_id, status, color, created_at, current_task "
                "FROM agents WHERE status != 'tombstone' "
                "ORDER BY created_at DESC"
            )
        else:
            cursor.execute(
                "SELECT agent_id, status, color, created_at, current_task "
                "FROM agents WHERE status = ? AND status != 'tombstone' "
                "ORDER BY created_at DESC",
                (status_filter,),
            )
        for row in cursor.fetchall():
            agent_dict = dict(row)
            # Wave 7 PR 2: presence signal sourced from the MCP
            # session registry (see _mcp_presence_for docstring).
            agent_dict.update(_mcp_presence_for(agent_dict['agent_id']))
            agents_list_data.append(agent_dict)
    except Exception as e:
        logger.error(f"Error fetching agents list: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse({'error': 'Failed to fetch agents list'}, status_code=500)
    finally:
        if conn:
            conn.close()
    return JSONResponse(agents_list_data)


# Wave 7 PR 3 (coordinator transition, 2026-06-29): the legacy
# ``create_agent_dashboard_api_route`` (which dispatched the spawn-
# claude-via-tmux ``create_agent_tool_impl``) is gone. The dashboard
# uses ``register_agent_dashboard_api_route`` below; the back-compat
# ``/api/create-agent`` alias route is dropped at the same time.

# ── Wave 7 PR 0: register-only flow (coordinator transition) ──
#
# Calls ``register_agent_tool_impl`` — register-only, no spawning.
# This is the only agent-creation route now that PR 3 has removed
# the legacy spawn surfaces.
#
# Shape: POST /api/agents/register with body
#   {"name": "<id>", "role": "worker"|"manager", "project_name": "...",
#    "host": "https://<dashboard origin>"}
# Returns 200 with
#   {"message": "...", "agent_id": "...", "agent_token": "...",
#    "mcp_snippet": "<json>"}
@router.post("/register")
async def register_agent_dashboard_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/agents/register — operator mints an agent identity.

    Wave 7 PR 0 (coordinator transition). Calls
    ``register_agent_tool_impl`` and surfaces the typed result the
    same way :func:`create_agent_dashboard_api_route` did — the
    dashboard's modal gets back a ``mcp_snippet`` it can render in
    the success pane.
    """

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e_val:
        return JSONResponse({"message": str(e_val)}, status_code=400)

    # Accept both the new ``name`` field (Wave 7 plan) and the legacy
    # ``agent_id`` shape so the existing dashboard request body still
    # works while the modal lands.
    name = data.get("name") or data.get("agent_id")
    role = data.get("role") or data.get("agent_role") or "worker"
    project_name = data.get("project_name")
    host = data.get("host")

    if not name:
        return JSONResponse(
            {"message": "`name` (agent_id) is required."},
            status_code=400,
        )
    if role not in ("worker", "manager"):
        return JSONResponse(
            {"message": (
                f"Invalid role {role!r}: must be 'worker' or 'manager'."
            )},
            status_code=422,
        )

    operator_id = caller_identity(auth)
    # AZ-R14-1 (round 14): thread the forwarding caller's REAL signed
    # ``(project_role, sysadmin)`` instead of a hard-coded ``"operator"``,
    # mirroring ``_dispatch_helpers._build_route_principal``. This was the
    # last per-project REST route that built its own ``operator_session``
    # Principal inline, so it never got the round-5 AC-R5-1 forwarding-role
    # threading — a forwarding VIEWER reaching here (should the router
    # method-gate / cookie-authorize ever be bypassed) would otherwise get
    # the full operator bundle, incl ``agents.register``. The carrier is
    # armed per-request by ``require_operator_session``'s forwarding branch;
    # the cookie / operator-tier bearer paths report ``None`` and keep the
    # historical operator-tier default (those admits are genuinely
    # operator).
    threaded = forwarding_route_role()
    project_role, sysadmin = threaded if threaded is not None else ("operator", False)
    # arch-B: build via the shared builder so caps resolve through the one
    # code path. The frontend supplies ``project_name`` explicitly — the
    # per-project backend doesn't yet derive its own project name from the
    # request; the Principal field is best-effort plumbing (the tool's
    # snippet builder reads ``arguments["project_name"]`` first either way).
    principal = build_operator_principal(
        user_id=operator_id,
        kind="operator_session",
        project_role=project_role,
        sysadmin=sysadmin,
        project_name=project_name if isinstance(project_name, str) else None,
        source_token=None,
    )

    tool_args = {
        "name": name,
        "role": role,
    }
    if isinstance(project_name, str) and project_name:
        tool_args["project_name"] = project_name
    if isinstance(host, str) and host:
        tool_args["host"] = host

    try:
        result = await register_agent_tool_impl(
            tool_args, principal=principal,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.error(
            "Error in register_agent_dashboard_api_route: %s", e,
            exc_info=True,
        )
        # BL-R5-2 / SD-R6-1: generic message — see fetch-agents-list note.
        return JSONResponse(
            {"message": "Error registering agent"}, status_code=500,
        )

    if isinstance(result, _Ok):
        # Bespoke Ok shaping: this route flattens ``Ok.data`` into the
        # named fields the dashboard's register modal reads (agent_token,
        # mcp_snippet, …). Preserved verbatim — only the ERROR status
        # mapping is unified below.
        payload = result.data if isinstance(result.data, dict) else {}
        return JSONResponse({
            "message": result.message
            or f"Agent '{name}' registered.",
            "agent_id": payload.get("agent_id"),
            "agent_token": payload.get("token"),
            "agent_role": payload.get("agent_role"),
            "mcp_snippet": payload.get("mcp_snippet"),
            "project_name": payload.get("project_name"),
        })

    # Error variants: STATUS comes from the ONE shared adapter
    # (:func:`agent_mcp.core.tool_result.tool_result_to_http`), replacing
    # the hand-rolled isinstance ladder that had drifted — it mapped
    # ``PermissionDenied → 401`` where the shared dispatcher maps 403.
    # 403 is correct: the caller authenticated but lacks the capability
    # (authenticated-but-forbidden); 401 is reserved for missing/invalid
    # credentials the auth middleware rejects upstream of dispatch. This
    # route keeps its thin ``{"message": ...}`` body (the dashboard's
    # register modal reads ``.message``); only the status is unified.
    status, body = tool_result_to_http(result)
    # ``Failed`` keeps this route's historical raw ``result.message`` in
    # the body — the adapter genericizes it (SEC-R8-1) for the dashboard
    # dispatch path, but preserving this route's exact wire text keeps the
    # change status-only + behaviour-preserving (register never surfaces a
    # DB-error Failed today; the outer except already genericizes RAISED
    # errors).
    message = result.message if isinstance(result, _Failed) else body["message"]
    return JSONResponse({"message": message}, status_code=status)


# --- Agent restore + edit + purge endpoints (E2: thin adapters) ---
# `terminate_agent` is a soft-delete; an operator then Restores (reverse
# soft-delete), Edits, or Purges (hard delete + cascade tombstone). E2
# (arch-deepening) extracted the logic into ``tools.admin_tools``
# (``restore_agent`` / ``edit_agent`` / ``purge_agent`` on the
# unit-of-work); the routes below are thin adapters that keep only the
# wire concerns (body sanitize, method/param guards, wire-level input
# hygiene) then dispatch + map the ToolResult to the legacy body shape.
# The cascade contract + tombstone helper live with the tool now.


async def _dispatch_agent_lifecycle_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    auth: dict,
) -> ToolResult | JSONResponse:
    """Dispatch an agent-lifecycle tool from a REST handler.

    Builds the operator-session Principal (AC-R5-1: a forwarding VIEWER
    gets a viewer-role Principal the tool's ``agents.terminate`` gate
    denies), dispatches, and returns the raw :data:`ToolResult` for the
    caller to shape into its legacy body. On a dispatch-level failure
    (input-validation / unexpected exception) returns a ready
    :class:`JSONResponse` instead — the caller returns it verbatim.
    """
    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )
    try:
        return await dispatch_tool_call(
            tool_name, arguments, principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return a static generic 500 (the caller supplies the wording).
        logger.error(
            "Error dispatching %s: %s", tool_name, e, exc_info=True,
        )
        return JSONResponse(
            {"error": f"Failed to dispatch {tool_name}"}, status_code=500,
        )


def _agent_tool_error(result: ToolResult, failed_message: str) -> JSONResponse:
    """Map a non-``Ok`` agent-lifecycle :data:`ToolResult` to the legacy
    ``{"error": ...}`` envelope these routes have always returned.

    STATUS comes from the shared C-wave adapter
    (:func:`tool_result_to_http`); the body wording comes from the ONE
    shared :func:`tool_result_error_message` mapper (arch-r4 #10 —
    this used to be a private re-implementation of the same variant
    ladder). ``not_found_label="Agent"`` (capitalized, not the tool's
    ``resource="agent"``) preserves this route's historical NotFound
    wording (the dashboard + the restore/edit/purge REST tests pin this
    shape). ``Failed`` (or any residual variant) renders the route's
    static generic ``failed_message`` (SEC — no exception-detail leak;
    the dispatcher already logged the real one).
    """
    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {
            "error": tool_result_error_message(
                result, failed_message, not_found_label="Agent",
            )
        },
        status_code=status,
    )


@router.post("/{agent_id}/restore")
async def restore_agent_api_route(
    agent_id: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/agents/<id>/restore — thin adapter over ``restore_agent``.

    E2 (arch-deepening): the reverse-soft-delete choreography (status flip
    + terminated_at clear + ``restored_agent`` audit + ``g.active_agents``
    / ``g.agent_working_dirs`` rebuild) lives ONCE in
    :func:`agent_mcp.tools.admin_tools.restore_agent_tool_impl` on the
    unit-of-work. This handler keeps only the wire concerns (legacy body
    read) then dispatches + maps the ToolResult to the legacy body shape.
    Auth stays operator-only via ``require_operator_session``.

    arch-r4 #10: ``agent_id`` is now a typed path parameter — FastAPI's
    own routing already guarantees a non-empty value (the ``str``
    convertor requires 1+ chars), so the guard below is unreachable in
    practice; kept as defensive belt-and-suspenders rather than deleted,
    since removing it risks nothing and changes nothing observable.
    """
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    # Body is read for shape-compat with legacy callers but no field is
    # required; the dep enforces auth.
    try:
        _ = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    result = await _dispatch_agent_lifecycle_tool(
        "restore_agent", {"agent_id": agent_id}, auth,
    )
    if isinstance(result, JSONResponse):
        return result
    if isinstance(result, _Ok):
        return JSONResponse({
            "success": True,
            "agent_id": result.data["agent_id"],
            "status": result.data["status"],
            "message": result.message or f"Agent '{agent_id}' restored",
        })
    return _agent_tool_error(result, "Failed to restore agent")


@router.post("/{agent_id}/edit")
async def edit_agent_api_route(
    agent_id: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/agents/<id>/edit — thin adapter over ``edit_agent``.

    Accepts any combination of the editable fields
    (:data:`agent_mcp.tools.admin_tools.EDITABLE_AGENT_FIELDS`):
    ``capabilities`` (list[str]), ``color`` (str), ``working_directory``
    (str), ``aoe_session_id`` (str), ``auto_event_loop`` (bool),
    ``agent_role`` ('worker'|'manager'). Returns 400 if none are supplied,
    404 if the agent does not exist. Non-whitelisted fields are ignored —
    status / agent_id / token have their own flows.

    E2 (arch-deepening): the apply choreography (field writes +
    ``edited_agent`` audit + cache refresh + auto_event_loop wake) lives
    ONCE in :func:`agent_mcp.tools.admin_tools.edit_agent_tool_impl` on the
    unit-of-work. This handler keeps only the wire concerns — body
    sanitize, the SEC-round-9 type-confusion guards, the ``agent_role``
    422, and the ``aoe_session_id`` format normalisation (all carry
    non-standard HTTP statuses / body wording the dashboard pins) — then
    dispatches the pre-validated fields and maps the ToolResult back to the
    legacy body. Auth stays operator-only via ``require_operator_session``.

    arch-r4 #10: ``agent_id`` is now a typed path parameter (see
    ``restore_agent_api_route`` for why the ``not agent_id`` guard below
    is unreachable-but-kept).
    """
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Whitelisted editable fields (shared source of truth with the tool).
    # Anything else in `data` is ignored (defense in depth — status /
    # agent_id / token must not flow through this endpoint).
    from ...tools.admin_tools import EDITABLE_AGENT_FIELDS

    updates = {k: data[k] for k in EDITABLE_AGENT_FIELDS if k in data}

    # agent_role validation is a wire-level 422 (Pydantic-equivalent) — the
    # CHECK constraint would otherwise surface as a 500. Kept here because
    # 422 is not a standard ToolResult status.
    if 'agent_role' in updates and updates['agent_role'] not in (
        'worker', 'manager',
    ):
        return JSONResponse(
            {"error": (
                f"Invalid agent_role {updates['agent_role']!r}: "
                "must be 'worker' or 'manager'."
            )},
            status_code=422,
        )

    if not updates:
        return JSONResponse(
            {"error": "No editable fields supplied. Accepts any of: "
                      + ", ".join(EDITABLE_AGENT_FIELDS)},
            status_code=400,
        )

    # SEC round-9: type-guard each editable field BEFORE it reaches a SQL
    # bind / normalize_capabilities / the g.active_agents cache. Wire-level
    # hygiene kept local per the round-9 scope — the MCP path validates the
    # same via the tool's inputSchema. ``capabilities`` must be a genuine
    # list[str] — a dict would have normalize_capabilities iterate its keys
    # and store them (the task-claim authz gate reads this cache), behind a
    # misleading 200. ``color`` / ``working_directory`` 500 on a dict/list
    # bind. ``auto_event_loop`` is a bool toggle — a dict/list/str is
    # truthy-coerced to a silent 1/0, so reject those.
    if 'capabilities' in updates:
        _err = _require_str_list(
            updates['capabilities'], "capabilities", allow_none=False,
        )
        if _err is not None:
            return _err
    for _field in ('color', 'working_directory'):
        if _field in updates:
            _err = _require_str(updates[_field], _field)
            if _err is not None:
                return _err
    if 'auto_event_loop' in updates and not isinstance(
        updates['auto_event_loop'], (bool, int)
    ):
        return JSONResponse(
            {"error": "auto_event_loop must be a boolean"},
            status_code=400,
        )

    # aoe_session_id: AoE generates 16-char lowercase hex ids. Accept that
    # exact shape or empty string (clears the binding, stored as NULL).
    # Anything else → 400. The clear case is normalised to the ``""``
    # sentinel (NOT ``None``): ``dispatch_tool_call`` strips top-level
    # ``None`` args (its ``{"token": null}`` handler), so a ``None`` here
    # would silently drop the field and never clear the column. The tool
    # maps ``""`` → NULL at write time.
    if 'aoe_session_id' in updates:
        raw = updates['aoe_session_id']
        if raw is None or raw == '':
            updates['aoe_session_id'] = ""
        elif (
            not isinstance(raw, str)
            or len(raw) != 16
            or any(c not in '0123456789abcdef' for c in raw)
        ):
            return JSONResponse(
                {"error": "aoe_session_id must be 16 lowercase hex chars "
                          "or empty (got " + repr(raw) + ")"},
                status_code=400,
            )

    result = await _dispatch_agent_lifecycle_tool(
        "edit_agent", {"agent_id": agent_id, **updates}, auth,
    )
    if isinstance(result, JSONResponse):
        return result
    if isinstance(result, _Ok):
        return JSONResponse({
            "success": True,
            "agent_id": result.data["agent_id"],
            "updated": result.data["updated"],
            "message": result.message or (
                f"Agent '{agent_id}' updated: "
                + ", ".join(result.data["updated"].keys())
            ),
        })
    return _agent_tool_error(result, "Failed to edit agent")


# ``_gather_purge_preview`` + ``_purge_tombstone`` moved to
# ``tools.admin_tools`` (E2) — the purge cascade logic now lives with the
# ``purge_agent`` tool; this route + the preview route import them.


@router.get("/{agent_id}/purge-preview")
async def purge_preview_api_route(
    agent_id: str,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/agents/<id>/purge-preview — blast-radius counts + samples.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    Dashboard no longer passes the admin token in the query string.

    arch-r4 #10: ``agent_id`` is now a typed path parameter (see
    ``restore_agent_api_route`` for why the ``not agent_id`` guard below
    is unreachable-but-kept).
    """
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM agents WHERE agent_id = ?", (agent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return JSONResponse(
                {"error": f"Agent '{agent_id}' not found"},
                status_code=404,
            )

        preview = _gather_purge_preview(cursor, agent_id)
        preview["agent_id"] = agent_id
        preview["status"] = row["status"]
        preview["tombstone"] = _purge_tombstone(agent_id)
        return JSONResponse(preview)
    except Exception as e:
        logger.error(
            f"Error computing purge preview for {agent_id}: {e}", exc_info=True,
        )
        # BL-R5-2 / SD-R6-1: generic message — see fetch-agents-list note.
        return JSONResponse(
            {"error": "Failed to compute purge preview"},
            status_code=500,
        )
    finally:
        if conn:
            conn.close()


@router.delete("/{agent_id}")
async def purge_agent_api_route(
    agent_id: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/agents/<id>?cascade=true — thin adapter over ``purge_agent``.

    Refuses without ?cascade=true so a bare DELETE doesn't silently
    hard-delete data (wire-level safety kept here).

    E2 (arch-deepening): the 6-table tombstone cascade (agents /
    agent_messages / tasks / agent_actions / mcp_sessions /
    claude_code_sessions, DELETE of the agents row LAST) lives ONCE in
    :func:`agent_mcp.tools.admin_tools.purge_agent_tool_impl` on the
    unit-of-work — one atomic transaction, in-memory reference drops +
    reassigned-task reconcile registered post-commit. This handler keeps
    only the wire concerns (the cascade=true confirmation, legacy body
    read) then dispatches + maps the ToolResult to the legacy body. Auth
    stays operator-only via ``require_operator_session``.

    arch-r4 #10: ``agent_id`` is now a typed path parameter (see
    ``restore_agent_api_route`` for why the ``not agent_id`` guard below
    is unreachable-but-kept).
    """
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    if request.query_params.get('cascade', '').lower() != 'true':
        return JSONResponse(
            {"error": "Refusing to hard-delete without cascade=true. "
                      "Pass ?cascade=true to confirm tombstone cascade."},
            status_code=400,
        )

    # Body is read for shape-compat with legacy callers but no field is
    # required; the dep enforces auth.
    try:
        _ = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    result = await _dispatch_agent_lifecycle_tool(
        "purge_agent", {"agent_id": agent_id}, auth,
    )
    if isinstance(result, JSONResponse):
        return result
    if isinstance(result, _Ok):
        return JSONResponse({
            "success": True,
            "agent_id": result.data["agent_id"],
            "tombstone": result.data["tombstone"],
            "counts": result.data["counts"],
            "message": result.message or f"Agent '{agent_id}' purged",
        })
    return _agent_tool_error(result, "Failed to purge agent")
