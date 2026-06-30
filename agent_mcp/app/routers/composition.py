"""Composition router — cross-resource reads + legacy verb-y URLs.

Wave 8 PR 1 of prancy-napping-pie hosts two distinct shapes here,
both unified by living on the bare ``/api`` prefix:

Cross-resource reads (genuine composition):
  * ``GET /api/status`` — system status
  * ``GET /api/graph-data`` — agents/tasks/files relationship graph
  * ``GET /api/task-tree-data`` — task tree
  * ``GET /api/node-details`` — node detail panel reads
  * ``GET /api/all-data`` — single-call dashboard hydration blob
  * ``GET /api/context-data`` — memory-only payload (URL doesn't
    match ``/api/memories`` so it stays here, not on the memories
    router)

Legacy verb-y URLs that don't match the semantic resource's prefix:
  * ``POST /api/terminate-agent`` — would semantically belong on
    the agents router, but its URL doesn't match ``/api/agents``.
  * ``POST /api/update-task-dashboard`` — would semantically belong
    on the tasks router, but its URL doesn't match ``/api/tasks``.
  * ``POST /api/create-sample-memories`` — would semantically
    belong on the memories router, but its URL doesn't match
    ``/api/memories``.

URL stability wins; a future PR can migrate these URLs to canonical
per-resource shapes alongside dashboard updates.

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _dispatch_through_tool, handle_options
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...core import globals as g
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...db.engine import SessionLocal
from ...db.models import ProjectContext
from ...features.dashboard.api import (
    fetch_graph_data_logic,
    fetch_task_tree_data_logic,
)
from ...utils.json_utils import get_sanitized_json_body
from .agents import _mcp_presence_for


router = APIRouter(
    prefix="/api",
    tags=["composition"],
)


# --- Composition reads (cross-resource) ---


@router.api_route("/status", methods=["GET", "OPTIONS"])
async def simple_status_api_route(request: Request) -> JSONResponse:
    # Handle OPTIONS for CORS preflight
    if request.method == 'OPTIONS':
        return await handle_options(request)

    try:
        # Get system status
        from ...db.actions.agent_db import get_all_active_agents_from_db
        from ...repositories import task_repo

        agents = get_all_active_agents_from_db()
        # PR #146: route the listing through the class-based
        # TaskRepository so future per-instance hooks (audit, metrics)
        # apply uniformly to dashboard reads too.
        tasks = task_repo.list_all()

        # Count task statuses
        pending_tasks = len([t for t in tasks if t.get('status') == 'pending'])
        completed_tasks = len([t for t in tasks if t.get('status') == 'completed'])

        return JSONResponse({
            "server_running": True,
            "total_agents": len(agents),
            "active_agents": len([a for a in agents if a.get('status') == 'active']),
            "total_tasks": len(tasks),
            "pending_tasks": pending_tasks,
            "completed_tasks": completed_tasks,
            "last_updated": datetime.datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error in simple_status_api_route: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to get simple status: {str(e)}"}, status_code=500)


@router.api_route("/graph-data", methods=["GET", "OPTIONS"])
async def graph_data_api_route(request: Request) -> JSONResponse:
    if request.method == 'OPTIONS':
        return await handle_options(request)
    try:
        data = await fetch_graph_data_logic(g.file_map.copy())
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving graph data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': str(e)}, status_code=500)


@router.api_route("/task-tree-data", methods=["GET", "OPTIONS"])
async def task_tree_data_api_route(request: Request) -> JSONResponse:
    if request.method == 'OPTIONS':
        return await handle_options(request)
    try:
        data = await fetch_task_tree_data_logic()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving task tree data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': str(e)}, status_code=500)


@router.api_route("/node-details", methods=["GET", "OPTIONS"])
async def node_details_api_route(request: Request) -> JSONResponse:
    if request.method == 'OPTIONS':
        return await handle_options(request)
    node_id = request.query_params.get('node_id')
    if not node_id:
        return JSONResponse({'error': 'Missing node_id parameter'}, status_code=400)
    details: Dict[str, Any] = {'id': node_id, 'type': 'unknown', 'data': {}, 'actions': [], 'related': {}}
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        parts = node_id.split('_', 1)
        node_type_from_id = parts[0] if len(parts) > 1 else node_id
        actual_id_from_node = parts[1] if len(parts) > 1 else (node_id if node_type_from_id != 'admin' else 'admin')
        details['type'] = node_type_from_id
        if node_type_from_id == 'agent':
            cursor.execute("SELECT * FROM agents WHERE agent_id = ?", (actual_id_from_node,))
            row = cursor.fetchone()
            if row:
                details['data'] = dict(row)
            cursor.execute("SELECT timestamp, action_type, task_id, details FROM agent_actions WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT task_id, title, status, priority FROM tasks WHERE assigned_to = ? ORDER BY created_at DESC LIMIT 10", (actual_id_from_node,))
            details['related']['assigned_tasks'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'task':
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (actual_id_from_node,))
            row = cursor.fetchone()
            if row:
                details['data'] = dict(row)
            cursor.execute("SELECT timestamp, agent_id, action_type, details FROM agent_actions WHERE task_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'context':
            cursor.execute("SELECT * FROM project_context WHERE context_key = ?", (actual_id_from_node,))
            row = cursor.fetchone()
            if row:
                details['data'] = dict(row)
            cursor.execute("SELECT timestamp, agent_id, action_type FROM agent_actions WHERE (action_type = 'updated_context' OR action_type = 'update_project_context') AND details LIKE ? ORDER BY timestamp DESC LIMIT 5", (f'%"{actual_id_from_node}"%',))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'file':
            details['data'] = {'filepath': actual_id_from_node, 'info': g.file_map.get(actual_id_from_node, {})}
            cursor.execute("SELECT timestamp, agent_id, action_type, details FROM agent_actions WHERE (action_type LIKE '%_file' OR action_type LIKE 'claim_file_%' OR action_type = 'release_file') AND details LIKE ? ORDER BY timestamp DESC LIMIT 5", (f'%"{actual_id_from_node}"%',))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'admin':
            details['data'] = {'name': 'Admin User / System'}
            cursor.execute("SELECT timestamp, action_type, task_id, details FROM agent_actions WHERE agent_id = 'admin' ORDER BY timestamp DESC LIMIT 10")
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        if not details.get('data') and node_type_from_id not in ['admin']:
            return JSONResponse({'error': 'Node data not found or type unrecognized'}, status_code=404)
    except Exception as e:
        logger.error(f"Error fetching details for node {node_id}: {e}", exc_info=True)
        return JSONResponse({'error': f'Failed to fetch node details: {str(e)}'}, status_code=500)
    finally:
        if conn:
            conn.close()
    return JSONResponse(details)


# --- Comprehensive Data Endpoint ---
# Default per-section LIMIT for /api/all-data; bounded by the 2026-06-02
# database review (item 2) so a project with thousands of rows no longer
# materialises an unbounded payload on every dashboard refresh. Callers
# that want more can pass `?limit=N`, but `_ALL_DATA_MAX_LIMIT` clamps
# the upper bound to keep the JSON shape sane.
_ALL_DATA_DEFAULT_LIMIT = 500
_ALL_DATA_MAX_LIMIT = 5000


@router.api_route("/all-data", methods=["GET", "OPTIONS"])
async def all_data_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Get all data in one call for caching on frontend.

    Wave 1 (prancy-napping-pie): cookie-migrated. Before this PR the
    endpoint had NO auth gate — anyone with network reach got the full
    dashboard hydration blob (agents, tasks, context, admin_token) for
    free. The dep accepts cookie / bearer / body-token / query-token
    just like every other migrated route in this file; see
    ``app/deps.py`` for the legacy fallback rationale.

    Wave 3 (prancy-napping-pie) dropped the legacy ``admin_token``
    field from the response and the synthesised ``Admin`` pseudo-agent
    row (which sourced ``auth_token`` from ``g.admin_token``). The
    dashboard no longer reads the field (Wave 2). The synthesised row
    will be replaced by the real ``admin`` row from the agents table
    once Wave 4 drops the admin pseudo-agent entirely.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Bound the per-section response so a project with thousands
        # of agents/tasks/file_metadata rows no longer ships an
        # unbounded blob on every dashboard refresh (db review item 2).
        # Default to `_ALL_DATA_DEFAULT_LIMIT`; allow `?limit=` to
        # override within `[1, _ALL_DATA_MAX_LIMIT]`.
        try:
            requested_limit = int(request.query_params.get("limit", _ALL_DATA_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            requested_limit = _ALL_DATA_DEFAULT_LIMIT
        section_limit = max(1, min(requested_limit, _ALL_DATA_MAX_LIMIT))

        # Build a single agent_id -> active-token map up front so the
        # per-agent token lookup below is O(1) instead of O(n²)
        # (db review item 9).
        active_token_by_agent: dict[str, str] = {}
        for token, data in g.active_agents.items():
            if data.get("status") == "terminated":
                continue
            ag_id = data.get("agent_id")
            if ag_id and ag_id not in active_token_by_agent:
                active_token_by_agent[ag_id] = token

        cursor.execute(
            "SELECT * FROM agents ORDER BY created_at DESC LIMIT ?",
            (section_limit,),
        )
        agents_data = []
        for row in cursor.fetchall():
            agent_dict = dict(row)
            # Defensive skip for the legacy 'admin' pseudo-agent row.
            # Wave 4 (migration 0014) deletes it; this filter remains
            # so a partially-upgraded DB (or one with the
            # AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP escape hatch leaving
            # the row behind) doesn't suddenly surface a stale entry.
            if agent_dict.get('agent_id') == 'admin':
                continue
            # Skip purge-cascade tombstone rows (status='tombstone',
            # agent_id like '[deleted-<original>]'). PR-G1 INSERTs
            # these so agent_messages.{sender_id,recipient_id} FK can
            # be UPDATE'd to point at the tombstone before the
            # original row is DELETE'd. The tombstone row is
            # load-bearing for FKs — it must stay in the DB — but it
            # has no business in the user-facing dashboard list.
            # Without this filter, purge never drops the dashboard's
            # visible agent count (the tombstone takes the place of
            # the deleted row), which silently violates the spec
            # contract "purge drops count by 1". See
            # tests/test_purge_drops_visible_count.py.
            if agent_dict.get('status') == 'tombstone':
                continue
            agent_dict['auth_token'] = active_token_by_agent.get(
                agent_dict['agent_id']
            )
            # Event-coord PR-3 (updated by PR-B / v5.0.24): surface
            # whether this agent currently has any in-flight
            # ``wait_for_events`` long-poll call. Pre-fan-out (PR-2)
            # this read ``g.lock_for(agent_id).locked()`` because
            # only one waiter at a time was permitted. After PR-B the
            # lock is gone — multiple concurrent waiters are
            # supported — so the flag now reflects "≥1 waiter parked"
            # via ``g.waiter_count()``. Dashboard + Settings page
            # consume the same boolean shape; semantics widen from
            # "this single call is in flight" to "at least one call
            # is in flight" without changing the contract.
            agent_dict['wait_for_events_in_flight'] = bool(
                g.waiter_count(agent_dict['agent_id']) > 0
            )
            # Wave 7 PR 2 — coordinator transition. Surface presence
            # (online + last_mcp_connection) for the dashboard agents
            # list so the badge can switch from spawn-lifecycle status
            # to live MCP-connection status. Same source as the
            # GET /api/agents endpoint.
            agent_dict.update(_mcp_presence_for(agent_dict['agent_id']))
            agents_data.append(agent_dict)

        # Wave 3 (prancy-napping-pie): the synthesised 'Admin' agent
        # row (which sourced ``auth_token`` from ``g.admin_token``) is
        # gone. The dashboard's hardcoded ``agent_id === 'Admin'``
        # defensive branches simply never match now — that's fine; they
        # were defensive (skip edit/terminate buttons for the admin
        # row), not load-bearing. Wave 4 deleted the underlying admin
        # pseudo-agent row entirely (migration 0014).
        #
        # Out-of-tree consumers that relied on the synthesised
        # 'Admin' row's ``auth_token`` field to harvest the system
        # token must migrate to per-agent bearer tokens — see
        # ``docs/external-mcp-client.md``. retire-system-token Wave 3
        # deleted the system token; there is no equivalent value.

        cursor.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (section_limit,),
        )
        tasks_data = [dict(row) for row in cursor.fetchall()]

        # Get bounded context entries via the ORM (Phase 7a; ownership cols 7b).
        with SessionLocal() as ctx_session:
            ctx_rows = (
                ctx_session.query(ProjectContext)
                .order_by(ProjectContext.updated_at.desc())
                .limit(section_limit)
                .all()
            )
            context_data = [
                {
                    "context_key": r.context_key,
                    "value": r.value,
                    "updated_at": r.updated_at,
                    "updated_by": r.updated_by,
                    "created_at": r.created_at,
                    "created_by": r.created_by,
                    "description": r.description,
                }
                for r in ctx_rows
            ]

        # Recent agent actions: capped at min(100, section_limit). Keeps
        # the pre-existing "last 100" behavior when no `?limit=` is
        # supplied; lets `?limit=N<100` shrink it further.
        actions_cap = min(100, section_limit)
        cursor.execute(
            "SELECT * FROM agent_actions ORDER BY timestamp DESC LIMIT ?",
            (actions_cap,),
        )
        actions_data = [dict(row) for row in cursor.fetchall()]

        # Get bounded file metadata (db review item 2 — was unbounded).
        cursor.execute(
            "SELECT * FROM file_metadata LIMIT ?",
            (section_limit,),
        )
        file_metadata = [dict(row) for row in cursor.fetchall()]

        response_data = {
            "agents": agents_data,
            "tasks": tasks_data,
            "context": context_data,
            "actions": actions_data,
            "file_metadata": file_metadata,
            "file_map": g.file_map,
            # Wave 3 (prancy-napping-pie): ``admin_token`` removed from
            # response. The dashboard no longer reads it (Wave 2).
            # retire-system-token Wave 3 deleted the underlying god-key;
            # external clients use per-agent bearers (see
            # docs/external-mcp-client.md).
            "timestamp": datetime.datetime.now().isoformat()
        }

        # VULN-001 (security audit 2026-06-29): static
        # ``Access-Control-Allow-Origin: *`` headers were dropped from
        # this credentialed endpoint. CORSMiddleware (configured in
        # :func:`agent_mcp.app.main_app.create_app`) now owns CORS
        # response headers and emits the correct per-origin reply
        # against the shared :data:`ALLOWED_ORIGINS` allowlist.
        return JSONResponse(response_data)

    except Exception as e:
        logger.error(f"Error fetching all data: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to fetch all data: {str(e)}"}, status_code=500)
    finally:
        if conn:
            conn.close()


@router.api_route("/context-data", methods=["GET", "OPTIONS"])
async def context_data_api_route(request: Request) -> JSONResponse:
    """Get only context data.

    URL placement note: this lives on the composition router
    (prefix ``/api``) rather than the memories router (prefix
    ``/api/memories``) because the URL is ``/api/context-data``,
    not ``/api/memories``. URL stability wins; a future PR can
    migrate to ``/api/memories`` GET alongside dashboard updates.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    try:
        with SessionLocal() as session:
            rows = (
                session.query(ProjectContext)
                .order_by(ProjectContext.updated_at.desc())
                .all()
            )
            context_data = [
                {
                    "context_key": r.context_key,
                    "value": r.value,
                    "updated_at": r.updated_at,
                    "updated_by": r.updated_by,
                    "created_at": r.created_at,
                    "created_by": r.created_by,
                    "description": r.description,
                }
                for r in rows
            ]

        # VULN-001 (security audit 2026-06-29): see /all-data above —
        # static wildcard CORS headers dropped; CORSMiddleware owns
        # the response shape now.
        return JSONResponse(context_data)

    except Exception as e:
        logger.error(f"Error fetching context data: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to fetch context data: {str(e)}"}, status_code=500)


# --- Legacy verb-y URLs placed here because the URL prefix doesn't
# --- match the semantic resource's router.


# Thin adapter (Candidate C, 2026-06-02 architecture review): dispatch
# through the `terminate_agent` MCP tool so validation +
# auth-rejection wording cannot drift between the dashboard surface
# and the MCP surface. Wire-shape parity is pinned by
# tests/test_rest_mcp_tool_parity.py.
@router.api_route("/terminate-agent", methods=["POST", "OPTIONS"])
async def terminate_agent_dashboard_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/terminate-agent — operator terminates an agent.

    Thin adapter over the ``terminate_agent`` MCP tool. The tool is
    gated by ``@requires_role("operator")``; this handler just
    translates the HTTP request → tool call → JSON response.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    Wave 3 (prancy-napping-pie): the inner tool call no longer
    synthesises ``g.admin_token`` as a bearer to satisfy the gate.
    Instead, ``_dispatch_through_tool`` stamps ``operator_session=True``
    on the dispatch — the decorator admits via the operator-session
    ContextVar (the route's outer dep has already validated the
    cookie / legacy bearer / body-token before we get here).

    Wave 8 PR 1 placement: lives on the composition router (not the
    agents router) because the URL is ``/api/terminate-agent`` and
    doesn't match the agents router's ``/api/agents`` prefix. URL
    stability is the constraint; the semantic awkwardness can be
    cleaned up by a future URL-migration PR alongside dashboard
    updates.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e_val:
        return JSONResponse({"message": str(e_val)}, status_code=400)

    agent_id = data.get("agent_id")
    return await _dispatch_through_tool(
        "terminate_agent",
        {"agent_id": agent_id} if agent_id else {},
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
        success_message=(
            f"Agent '{agent_id}' terminated successfully via dashboard API."
            if agent_id else None
        ),
    )


@router.api_route("/update-task-dashboard", methods=["POST", "OPTIONS"])
async def update_task_details_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Dashboard task edit endpoint.

    Originally required (task_id, status) and used `status` as the
    only mandatory field. The dashboard's Edit modal needs to mutate
    individual fields independently (title-only, priority-only, …)
    and to manage assignment, so the rules are:
      - task_id still required.
      - status is now OPTIONAL (status-only updates still supported).
      - At least one editable field must be supplied (otherwise the
        UPDATE is a no-op and 400 is clearer than success).
      - assigned_to: new field. <agent_id> assigns; null/empty
        string clears the assignment (NULL in DB).

    PR D (prancy-napping-pie): auth moved to require_operator_session.
    The handler no longer reads or verifies an admin token from the
    body; the dep accepts cookie OR Authorization-bearer OR
    legacy body-token paths.

    Wave 8 PR 1 placement: lives on the composition router (not the
    tasks router) because the URL is ``/api/update-task-dashboard``
    and doesn't match the tasks router's ``/api/tasks`` prefix. URL
    stability is the constraint; future PRs can migrate to ``PATCH
    /api/tasks/{task_id}``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    conn = None
    try:
        import sqlite3
        data = await get_sanitized_json_body(request)
        task_id_to_update = data.get('task_id')
        new_status = data.get('status')
        if not task_id_to_update:
            return JSONResponse({"error": "task_id is a required field."}, status_code=400)
        # The set of recognised editable fields. At least one must be
        # supplied; otherwise the request is a no-op and rejected.
        EDITABLE_KEYS = {"status", "title", "description", "priority", "notes", "assigned_to"}
        supplied_editable = [k for k in EDITABLE_KEYS if k in data]
        if not supplied_editable:
            return JSONResponse(
                {"error": "at least one editable field is required (status, title, description, priority, notes, assigned_to)."},
                status_code=400,
            )
        requesting_admin_id = caller_identity(auth)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT notes FROM tasks WHERE task_id = ?", (task_id_to_update,))
        task_row = cursor.fetchone()
        if not task_row:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        existing_notes_str = task_row["notes"]
        # PR 7 (Task flip): build the field dict the same way the
        # legacy code built its SET clause, then hand it to
        # task_repo.update_fields(connection=cursor). The repo's
        # _MUTABLE_FIELDS allowlist + JSON-serialisation rule (for
        # `notes`) live in one place now; the route stops carrying
        # them as inline SQL fragments.
        fields_to_update: Dict[str, Any] = {}
        log_details: Dict[str, Any] = {}
        if new_status:
            fields_to_update["status"] = new_status
            log_details["status_updated_to"] = new_status
        if 'title' in data and data['title'] is not None:
            fields_to_update["title"] = data['title']
            log_details["title_changed"] = True
        if 'description' in data and data['description'] is not None:
            fields_to_update["description"] = data['description']
            log_details["description_changed"] = True
        if 'priority' in data and data['priority']:
            fields_to_update["priority"] = data['priority']
            log_details["priority_changed"] = True
        if 'assigned_to' in data:
            # null / '' / 'unassigned' clears the assignment; any other
            # value is stored verbatim as the agent_id.
            raw_assigned = data['assigned_to']
            new_assigned: Any
            if raw_assigned is None or (isinstance(raw_assigned, str) and raw_assigned.strip() in ('', 'unassigned')):
                new_assigned = None
            else:
                new_assigned = str(raw_assigned).strip()
            fields_to_update["assigned_to"] = new_assigned
            log_details["assigned_to_changed"] = new_assigned
        if 'notes' in data and data['notes'] and isinstance(data['notes'], str) and data['notes'].strip():
            try:
                current_notes_list = json.loads(existing_notes_str or "[]")
            except json.JSONDecodeError:
                current_notes_list = []
            new_note_entry = {"timestamp": datetime.datetime.now().isoformat(), "author": requesting_admin_id, "content": data['notes'].strip()}
            current_notes_list.append(new_note_entry)
            # The repo serialises list fields with json.dumps internally;
            # pass the Python list directly.
            fields_to_update["notes"] = current_notes_list
            log_details["notes_added"] = True
        if fields_to_update:
            from ...repositories import task_repo as _task_repo
            _task_repo.update_fields(
                task_id_to_update,
                fields_to_update,
                connection=cursor,
            )
        log_agent_action_to_db(cursor, requesting_admin_id, "updated_task_dashboard", task_id=task_id_to_update, details=log_details)
        conn.commit()
        if task_id_to_update in g.tasks:
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id_to_update,))
            updated_task_for_cache = cursor.fetchone()
            if updated_task_for_cache:
                g.tasks[task_id_to_update] = dict(updated_task_for_cache)
                for field_key in ["child_tasks", "depends_on_tasks", "notes"]:
                    if isinstance(g.tasks[task_id_to_update].get(field_key), str):
                        try:
                            g.tasks[task_id_to_update][field_key] = json.loads(g.tasks[task_id_to_update][field_key] or "[]")
                        except json.JSONDecodeError:
                            g.tasks[task_id_to_update][field_key] = []
            else:
                del g.tasks[task_id_to_update]
        return JSONResponse({"success": True, "message": "Task updated successfully via dashboard."})
    except ValueError as e_val:
        return JSONResponse({"error": str(e_val)}, status_code=400)
    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(f"DB error updating task via dashboard: {e_sql}", exc_info=True)
        return JSONResponse({"error": f"Failed to update task (DB): {str(e_sql)}"}, status_code=500)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error updating task via dashboard: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to update task: {str(e)}"}, status_code=500)
    finally:
        if conn:
            conn.close()


# --- Test/Demo Data Endpoint ---
@router.api_route("/create-sample-memories", methods=["POST", "OPTIONS"])
async def create_sample_memories_route(request: Request) -> JSONResponse:
    """Create sample memory entries for testing.

    Wave 8 PR 1 placement: lives on the composition router (not the
    memories router) because the URL is ``/api/create-sample-memories``
    and doesn't match the memories router's ``/api/memories`` prefix.
    URL stability is the constraint; a future PR could migrate to
    ``POST /api/memories/sample`` alongside dashboard updates.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    session = SessionLocal()
    try:
        # Sample memory entries
        sample_memories = [
            {
                'context_key': 'api.config.base_url',
                'value': json.dumps('https://api.example.com'),
                'description': 'Main API base URL for external services',
                'updated_by': 'system'
            },
            {
                'context_key': 'app.settings.theme',
                'value': json.dumps({'theme': 'dark', 'accent': 'blue'}),
                'description': 'Application theme preferences',
                'updated_by': 'admin'
            },
            {
                'context_key': 'database.connection.timeout',
                'value': json.dumps(30),
                'description': 'Database connection timeout in seconds',
                'updated_by': 'system'
            },
            {
                'context_key': 'cache.redis.config',
                'value': json.dumps({
                    'host': 'localhost',
                    'port': 6379,
                    'ttl': 3600
                }),
                'description': 'Redis cache configuration',
                'updated_by': 'admin'
            }
        ]

        current_time = datetime.datetime.now().isoformat()
        created_count = 0

        for memory in sample_memories:
            existing = (
                session.query(ProjectContext)
                .filter(ProjectContext.context_key == memory['context_key'])
                .one_or_none()
            )
            if existing is None:
                session.add(
                    ProjectContext(
                        context_key=memory['context_key'],
                        value=memory['value'],
                        created_at=current_time,
                        updated_at=current_time,
                        updated_by=memory['updated_by'],
                        description=memory['description'],
                    )
                )
            else:
                existing.value = memory['value']
                existing.updated_at = current_time
                existing.updated_by = memory['updated_by']
                existing.description = memory['description']
            created_count += 1

        session.commit()

        return JSONResponse({
            "success": True,
            "message": f"Created {created_count} sample memory entries",
            "created_count": created_count
        })

    except Exception as e:
        session.rollback()
        logger.error(f"Error creating sample memories: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)
    finally:
        session.close()
