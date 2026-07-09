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

import datetime
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import handle_options
from ..deps import (
    caller_identity,
    forwarding_route_role,
    require_operator_session,
)
from ...core.config import logger
from ...core import globals as g
from ...core import session_registry
from ...core.principal import Principal
from ...core.tool_result import (
    Conflict as _Conflict,
    Failed as _Failed,
    Invalid as _Invalid,
    NotFound as _NotFound,
    Ok as _Ok,
    PermissionDenied as _PermissionDenied,
)
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...tools.admin_tools import register_agent_tool_impl
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
# authz gate consumes. Guard every editable field up front. Kept local to
# this router per the round-9 scope.
def _require_str(value, field):
    """Return a 400 JSONResponse if ``value`` is present but not a str."""
    if value is not None and not isinstance(value, str):
        return JSONResponse(
            {"error": f"{field} must be a string"}, status_code=400
        )
    return None


def _require_str_list(value, field):
    """Return a 400 JSONResponse unless ``value`` is a list[str]."""
    if not isinstance(value, list) or not all(
        isinstance(x, str) for x in value
    ):
        return JSONResponse(
            {"error": f"{field} must be a list of strings"},
            status_code=400,
        )
    return None


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


@router.api_route("", methods=["GET", "OPTIONS"])
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
    if request.method == 'OPTIONS':
        return await handle_options(request)

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
@router.api_route("/register", methods=["POST", "OPTIONS"])
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
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

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
    # operator). We thread inline rather than call ``_build_route_principal``
    # because this route needs the bespoke ``project_name`` field the helper
    # doesn't carry (Principal is frozen — it can't be set after the fact).
    threaded = forwarding_route_role()
    project_role, sysadmin = threaded if threaded is not None else ("operator", False)
    principal = Principal(
        kind="operator_session",
        user_id=operator_id,
        agent_id=None,
        sysadmin=sysadmin,
        # The frontend supplies ``project_name`` explicitly — the
        # per-project backend doesn't yet derive its own project
        # name from the request. The Principal field is best-effort
        # plumbing; the tool's snippet builder reads
        # ``arguments["project_name"]`` first either way.
        project_name=project_name if isinstance(project_name, str) else None,
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
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
    if isinstance(result, _Conflict):
        return JSONResponse({"message": result.reason}, status_code=409)
    if isinstance(result, _NotFound):
        text = f"{result.resource} {result.identifier!r} not found."
        return JSONResponse({"message": text}, status_code=404)
    if isinstance(result, _Invalid):
        return JSONResponse({"message": result.message}, status_code=400)
    if isinstance(result, _PermissionDenied):
        return JSONResponse({"message": result.reason}, status_code=401)
    if isinstance(result, _Failed):
        return JSONResponse({"message": result.message}, status_code=500)
    return JSONResponse(
        {"message": f"Unknown tool result: {result!r}"}, status_code=500,
    )


# --- Agent restore + purge endpoints ---
# `terminate_agent` is a soft-delete: it flips status='terminated' but
# leaves the row + tokens + messages + tasks intact. Admins then either
# Restore (reverse soft-delete) or Purge (hard delete + cascade
# tombstone rewrite). Cascade table:
#
#   agents          → DELETE row (last in tx)
#   agent_messages  → tombstone sender_id/recipient_id → [deleted-<id>]
#   tasks           → tombstone created_by; SET NULL assigned_to + status=unassigned
#   agent_actions   → tombstone agent_id
#   tasks.notes JSON → UNTOUCHED — preserved as audit trail
#
# Tombstone format `[deleted-<id>]` depends on `[`/`]` being absent
# from real agent_ids; see create_agent_tool_impl validation.


def _purge_tombstone(agent_id: str) -> str:
    """Tombstone literal used to rewrite references to a purged agent."""
    return f"[deleted-{agent_id}]"


@router.api_route("/{agent_id}/restore", methods=["POST", "OPTIONS"])
async def restore_agent_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/agents/<id>/restore — admin reverses a soft-delete.

    Side effects of the original terminate (cleared current_task,
    released held files, killed tmux session) are NOT undone. Admin
    reassigns work explicitly. We only flip status back and re-add to
    g.active_agents so the dashboard's active-list/token-list pick it
    up again.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    agent_id = request.path_params.get('agent_id')
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    conn = None
    try:
        # Body is read for shape-compat with legacy callers but no
        # field is required; the dep enforces auth.
        try:
            _ = await get_sanitized_json_body(request)
        except ValueError:
            pass

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token, status FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return JSONResponse(
                {"error": f"Agent '{agent_id}' not found"},
                status_code=404,
            )
        if row["status"] != "terminated":
            return JSONResponse(
                {"error": f"Agent '{agent_id}' is not terminated "
                          f"(status={row['status']!r}); nothing to restore"},
                status_code=409,
            )

        agent_token = row["token"]
        # PR 6 + PR 8 (Agent flip): restore goes through
        # agent_repo.update_field with caller's cursor — atomic with
        # the audit log INSERT below. ``terminated_at`` was added to
        # the allowlist in PR 8 so the second field clear also goes
        # through the repo instead of owning a raw UPDATE on the
        # cursor. update_field accepts one field at a time, so two
        # calls; both share the caller's cursor so they stay inside
        # the wider BEGIN/COMMIT.
        from ...repositories import agent_repo as _agent_repo
        _agent_repo.update_field(
            agent_id, "status", "created", connection=cursor,
        )
        _agent_repo.update_field(
            agent_id, "terminated_at", None, connection=cursor,
        )
        log_agent_action_to_db(
            cursor, caller_identity(auth), "restored_agent",
            details={"agent_id": agent_id},
        )
        conn.commit()

        # Re-add to in-memory active map so the dashboard sees them.
        # We rebuild the entry from DB-known fields; capabilities/color
        # are not surfaced through this re-add path (admin can fetch
        # via /api/all-data if needed).
        cursor.execute(
            "SELECT agent_id, capabilities, created_at, status, color, "
            "working_directory, terminated_at, updated_at, current_task, "
            "agent_role "
            "FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        full = cursor.fetchone()
        if full is not None:
            try:
                caps = json.loads(full["capabilities"] or "[]")
            except (TypeError, json.JSONDecodeError):
                caps = []
            # SECURITY (terminate-revocation, related): rebuild the FULL
            # cache row including agent_role. Omitting it made a restored
            # manager transiently resolve to worker capabilities (a
            # privilege downgrade) until the next lifespan reload.
            g.active_agents[agent_token] = {
                "token": agent_token,
                "agent_id": full["agent_id"],
                "capabilities": caps,
                "created_at": full["created_at"],
                "status": full["status"],
                "color": full["color"],
                "working_directory": full["working_directory"],
                "terminated_at": full["terminated_at"],
                "updated_at": full["updated_at"],
                "current_task": full["current_task"],
                "agent_role": full["agent_role"],
            }

            # BL-R13-2: working_directory has a SECOND in-memory view —
            # g.agent_working_dirs (keyed by agent_id), which
            # get_working_directory() reads FIRST and returns on a
            # non-None hit. The active_agents restore above (keyed by
            # token) never reaches it, so after a restore the file tools +
            # get_agent_details keep resolving against stale/missing dir
            # data. Mirror the BL-R11-1 edit-path reconcile (and the
            # server_lifecycle warm-from-DB) for the restored agent.
            g.agent_working_dirs[agent_id] = full["working_directory"]

        return JSONResponse({
            "success": True,
            "agent_id": agent_id,
            "status": "created",
            "message": f"Agent '{agent_id}' restored",
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error restoring agent {agent_id}: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see fetch-agents-list note.
        return JSONResponse(
            {"error": "Failed to restore agent"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()


@router.api_route("/{agent_id}/edit", methods=["POST", "OPTIONS"])
async def edit_agent_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/agents/<id>/edit — admin updates mutable agent fields.

    Accepts any combination of the editable fields: ``capabilities``
    (list[str]), ``color`` (str), ``working_directory`` (str),
    ``aoe_session_id`` (str), ``auto_event_loop`` (bool). Returns
    400 if none of the editable fields are supplied (avoids no-op
    writes), 404 if the agent does not exist.

    Non-whitelisted fields in the body are silently ignored — the
    endpoint never touches status/agent_id/token; those have their own
    dedicated flows (terminate/restore/purge for status; create for
    agent_id+token; nothing for editing tokens).

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    agent_id = request.path_params.get('agent_id')
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    conn = None
    try:
        data = await get_sanitized_json_body(request)

        # Whitelisted editable fields. Anything else in `data` is ignored
        # (defense in depth — status / agent_id / token must not flow
        # through this endpoint).
        #
        # Event-coord PR-1: `auto_event_loop` (per-agent wake-loop
        # toggle) joins the editable list — dashboard's agent-edit
        # modal flips it to opt this agent out of the wake-loop
        # bootstrap shipped in PR-2.
        #
        # Phase 2 Wave 2b (plan §2e): `agent_role` joins the editable
        # list so the dashboard's Edit Agent modal can promote a
        # worker to manager (or demote). The Pydantic-equivalent
        # validation lives just below — rejecting anything outside
        # {worker, manager} with 422 so the CHECK constraint never
        # surfaces as a 500.
        editable = (
            'capabilities', 'color', 'working_directory', 'aoe_session_id',
            'auto_event_loop', 'agent_role',
        )
        updates = {k: data[k] for k in editable if k in data}

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
                          + ", ".join(editable)},
                status_code=400,
            )

        # SEC round-9: type-guard each editable field BEFORE it reaches a
        # SQL bind / normalize_capabilities / the g.active_agents cache.
        # ``capabilities`` must be a genuine list[str] — a dict here would
        # have normalize_capabilities iterate its keys and store them as
        # capabilities (the task-claim authz gate reads this cache), all
        # behind a misleading 200. ``color`` / ``working_directory`` 500
        # on a dict/list bind. ``auto_event_loop`` is a bool toggle — a
        # dict/list/str is truthy-coerced to a silent 1/0, so reject those.
        if 'capabilities' in updates:
            _err = _require_str_list(updates['capabilities'], "capabilities")
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

        # aoe_session_id: AoE generates 16-char lowercase hex ids.
        # Accept that exact shape or empty string (clears the binding,
        # stored as NULL in the column). Anything else → 400.
        if 'aoe_session_id' in updates:
            raw = updates['aoe_session_id']
            if raw is None or raw == '':
                updates['aoe_session_id'] = None
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

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token, status FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return JSONResponse(
                {"error": f"Agent '{agent_id}' not found"},
                status_code=404,
            )

        # PR 6: route field updates through agent_repo with the
        # caller's cursor so each update + the audit log INSERT below
        # land in the same transaction. The repo's allowlist + JSON
        # serialisation rules mirror the legacy update_agent_db_field
        # behaviour 1:1.
        from ...repositories import agent_repo as _agent_repo

        applied: Dict[str, Any] = {}
        for field, value in updates.items():
            result = _agent_repo.update_field(
                agent_id, field, value, connection=cursor,
            )
            if result is None:
                return JSONResponse(
                    {"error": f"Failed to update field {field!r}"},
                    status_code=500,
                )
            applied[field] = value

        # Refresh the in-memory active agent entry so the dashboard sees
        # the new color/capabilities without a server restart.
        agent_token = row["token"]
        if agent_token in g.active_agents:
            for field, value in applied.items():
                g.active_agents[agent_token][field] = value

        # BL-R11-1: working_directory has a SECOND in-memory view —
        # g.agent_working_dirs (keyed by agent_id), which
        # get_working_directory() reads FIRST and returns on a non-None
        # hit. The active_agents reconcile above (keyed by token) never
        # reaches it, so file tools + get_agent_details keep resolving
        # against the stale dir. Mirror the sibling reconcile.
        if "working_directory" in applied:
            g.agent_working_dirs[agent_id] = applied["working_directory"]

        # PR-2 event-coord: if `auto_event_loop` was flipped, wake any
        # in-flight wait_for_events for this agent so it re-evaluates
        # the flag state. The wait_for_events impl returns
        # `stop_listening` when the new state is OFF.
        if "auto_event_loop" in applied:
            try:
                g.wake_for_flag_recheck(agent_id)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "wake_for_flag_recheck(%s) failed after toggle: %s",
                    agent_id, e,
                )

        log_agent_action_to_db(
            cursor, caller_identity(auth), "edited_agent",
            details={"agent_id": agent_id, "fields": list(applied.keys())},
        )
        conn.commit()

        return JSONResponse({
            "success": True,
            "agent_id": agent_id,
            "updated": applied,
            "message": f"Agent '{agent_id}' updated: "
                       + ", ".join(applied.keys()),
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error editing agent {agent_id}: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see fetch-agents-list note.
        return JSONResponse(
            {"error": "Failed to edit agent"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()


def _gather_purge_preview(cursor, agent_id: str) -> Dict[str, Any]:
    """Compute the blast-radius counts + samples for a future purge.

    PR 6: message counts + sample go through ``message_repo`` so the
    repo owns the agent_messages query surface. The task / agent_actions
    counts stay on the cursor — they live in tables the message repo
    doesn't own, and the surrounding purge cascade is a multi-table
    transaction the cursor still drives.
    """
    from ...repositories import message_repo

    messages_sent = message_repo.count_query({"from": agent_id})
    messages_received = message_repo.count_query({"to": agent_id})
    cursor.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE created_by = ?",
        (agent_id,),
    )
    tasks_created = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_to = ?",
        (agent_id,),
    )
    tasks_assigned = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT COUNT(*) AS n FROM agent_actions WHERE agent_id = ?",
        (agent_id,),
    )
    agent_actions = cursor.fetchone()["n"]

    # Samples (most-recent first; small enough to inline in a modal).
    def _trim(s: str | None, n: int = 80) -> str:
        if not s:
            return ""
        return s if len(s) <= n else s[:n] + "..."

    sample_messages_sent = [
        {"content": _trim(m["message_content"]),
         "timestamp": m["timestamp"]}
        for m in message_repo.query(
            {"from": agent_id, "limit": 3, "offset": 0}
        )
    ]
    cursor.execute(
        "SELECT title FROM tasks WHERE created_by = ? "
        "ORDER BY created_at DESC LIMIT 3",
        (agent_id,),
    )
    sample_tasks_created = [r["title"] for r in cursor.fetchall()]
    cursor.execute(
        "SELECT title FROM tasks WHERE assigned_to = ? "
        "ORDER BY created_at DESC LIMIT 3",
        (agent_id,),
    )
    sample_tasks_assigned = [r["title"] for r in cursor.fetchall()]

    return {
        "counts": {
            "messages_sent": messages_sent,
            "messages_received": messages_received,
            "tasks_created": tasks_created,
            "tasks_assigned": tasks_assigned,
            "agent_actions": agent_actions,
        },
        "samples": {
            "messages_sent": sample_messages_sent,
            "tasks_created": sample_tasks_created,
            "tasks_assigned": sample_tasks_assigned,
        },
    }


@router.api_route("/{agent_id}/purge-preview", methods=["GET", "OPTIONS"])
async def purge_preview_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/agents/<id>/purge-preview — blast-radius counts + samples.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    Dashboard no longer passes the admin token in the query string.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'GET':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    agent_id = request.path_params.get('agent_id')
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


@router.api_route("/{agent_id}", methods=["DELETE", "OPTIONS"])
async def purge_agent_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/agents/<id>?cascade=true — hard delete + cascade tombstone.

    Wraps the cascade in a transaction (BEGIN/COMMIT) so a
    half-purged state is impossible if any step fails. The DELETE on
    agents runs LAST so logical references can be tombstoned while the
    row is still present (no DB foreign keys, but this preserves
    intent-readability).

    Refuses without ?cascade=true so a bare DELETE doesn't silently
    hard-delete data.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'DELETE':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    agent_id = request.path_params.get('agent_id')
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    if request.query_params.get('cascade', '').lower() != 'true':
        return JSONResponse(
            {"error": "Refusing to hard-delete without cascade=true. "
                      "Pass ?cascade=true to confirm tombstone cascade."},
            status_code=400,
        )

    conn = None
    try:
        # Body is read for shape-compat with legacy callers but no
        # field is required; the dep enforces auth.
        try:
            _ = await get_sanitized_json_body(request)
        except ValueError:
            pass

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token FROM agents WHERE agent_id = ?", (agent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return JSONResponse(
                {"error": f"Agent '{agent_id}' not found"},
                status_code=404,
            )
        agent_token = row["token"]

        # Snapshot counts before tombstoning so the response reflects
        # what we actually rewrote.
        preview = _gather_purge_preview(cursor, agent_id)
        counts = preview["counts"]

        tombstone = _purge_tombstone(agent_id)

        # Cascade — wrapped in an explicit transaction. sqlite3's
        # default isolation level already implicitly opens one on
        # mutation, but we use BEGIN/COMMIT for self-documenting intent
        # and to make rollback unambiguous.
        cursor.execute("BEGIN")
        try:
            # PR-G1: agent_messages.{sender_id, recipient_id} now FK to
            # agents.agent_id. The tombstone string `[deleted-<id>]`
            # must therefore exist as an `agents` row before any UPDATE
            # can rewrite a sender_id/recipient_id to it. INSERT OR
            # IGNORE so a re-purge (same agent_id, already tombstoned)
            # is a no-op. The token PK is namespaced under
            # `__tombstone_` so it can't collide with a real bearer.
            #
            # PR 8 (Agent flip): goes through
            # agent_repo.insert_tombstone with the caller's cursor so
            # the wider purge transaction (FK rewrites across
            # agent_messages / tasks / agent_actions, then the DELETE
            # of the original agents row) stays atomic.
            from ...repositories import agent_repo as _agent_repo
            _agent_repo.insert_tombstone(
                token=f"__tombstone_{agent_id}",
                tombstone_agent_id=tombstone,
                connection=cursor,
            )
            # PR 6: tombstone rewrite goes through message_repo with
            # the caller's cursor so the wider BEGIN/COMMIT cascade
            # stays atomic. The repo's transaction-aware seam tolerates
            # a sqlite3 cursor on the `connection=` kwarg.
            from ...repositories import message_repo as _msg_repo
            _msg_repo.rename_participant(
                agent_id, tombstone, connection=cursor,
            )
            cursor.execute(
                "UPDATE tasks SET created_by = ? WHERE created_by = ?",
                (tombstone, agent_id),
            )
            # Reassignment: anything assigned to this agent becomes
            # unassigned (admin can pick it up + reassign).
            #
            # BL-R10-1/2: capture the affected rows (with their
            # required_capabilities) BEFORE the UPDATE so we can
            # reconcile the g.tasks cache + wake workers post-commit. We
            # also bump updated_at so the catch-up feed
            # (``_collect_unassigned_task_events_for``, keyed on
            # updated_at) surfaces a task that TRANSITIONED to unassigned
            # after a disconnected worker's cursor.
            cursor.execute(
                "SELECT task_id, required_capabilities FROM tasks "
                "WHERE assigned_to = ?",
                (agent_id,),
            )
            reassigned_tasks = [
                (r["task_id"], r["required_capabilities"])
                for r in cursor.fetchall()
            ]
            cursor.execute(
                "UPDATE tasks SET assigned_to = NULL, status = 'unassigned', "
                "updated_at = ? WHERE assigned_to = ?",
                (datetime.datetime.now().isoformat(), agent_id),
            )
            cursor.execute(
                "UPDATE agent_actions SET agent_id = ? WHERE agent_id = ?",
                (tombstone, agent_id),
            )
            # BL-R4-2: mcp_sessions.agent_id and
            # claude_code_sessions.agent_id are FKs to agents.agent_id
            # (migrations 0007/0008). On a migration-built DB the FK is
            # enforced, so a session row still referencing this agent at
            # DELETE time makes the final `DELETE FROM agents` raise
            # `FOREIGN KEY constraint failed` and roll back the whole
            # purge. A purged agent's sessions are dead anyway, so DELETE
            # them here — in the same transaction, BEFORE the agents-row
            # delete below. Guarded on table presence so the purge still
            # works on an older schema that predates these tables.
            for _session_table in ("mcp_sessions", "claude_code_sessions"):
                cursor.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (_session_table,),
                )
                if cursor.fetchone() is not None:
                    cursor.execute(
                        f"DELETE FROM {_session_table} WHERE agent_id = ?",
                        (agent_id,),
                    )
            # Audit the purge itself — written *before* the agent row
            # disappears so the action log has a non-tombstoned
            # 'purged_agent' entry attributable to admin.
            log_agent_action_to_db(
                cursor, caller_identity(auth), "purged_agent",
                details={
                    "agent_id": agent_id,
                    "tombstone": tombstone,
                    "counts": counts,
                },
            )
            # DELETE the agents row LAST. PR 8 (Agent flip): goes
            # through agent_repo.delete with the caller's cursor so
            # cache eviction is owned by the repo (the explicit cache
            # pops below still run because the caller knows the
            # ``agent_token`` and doesn't want to depend on the repo's
            # post-commit scan).
            _agent_repo.delete(agent_id, connection=cursor)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Drop in-memory references.
        if agent_token in g.active_agents:
            del g.active_agents[agent_token]
        if agent_id in g.agent_working_dirs:
            del g.agent_working_dirs[agent_id]
        # Drop g.file_map entries held by this agent (cheap, idempotent).
        for filepath, info in list(g.file_map.items()):
            if info.get("agent_id") == agent_id:
                del g.file_map[filepath]

        # BL-R10-1/2: reconcile the reassigned tasks' g.tasks cache
        # entries + wake capability-matched workers. Shares the
        # terminate cascade's helper so both paths reconcile identically.
        from ...tools.admin_tools import _reconcile_reassigned_tasks
        _reconcile_reassigned_tasks(reassigned_tasks)

        return JSONResponse({
            "success": True,
            "agent_id": agent_id,
            "tombstone": tombstone,
            "counts": counts,
            "message": f"Agent '{agent_id}' purged",
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error(f"Error purging agent {agent_id}: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see fetch-agents-list note.
        return JSONResponse(
            {"error": "Failed to purge agent"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()
