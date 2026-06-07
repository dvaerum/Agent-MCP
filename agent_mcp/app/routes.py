# Agent-MCP/mcp_template/mcp_server_src/app/routes.py
import os
import json
import datetime
import sqlite3
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional # Added List, Dict, Any, Optional

from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.responses import JSONResponse, Response, PlainTextResponse
from starlette.requests import Request

# Project-specific imports
from ..core.config import logger
from ..core import globals as g
from ..core.auth import verify_token, get_agent_id as auth_get_agent_id
from ..utils.json_utils import get_sanitized_json_body
from ..db.connection import get_db_connection
from ..db.engine import SessionLocal
from ..db.models import ProjectContext
from ..db.actions.agent_actions_db import log_agent_action_to_db

from ..features.dashboard.api import (
    fetch_graph_data_logic,
    fetch_task_tree_data_logic
)
from ..features.dashboard.styles import get_node_style

# Import tool implementations that the dashboard APIs which still
# call directly (vs. dispatch through MCP) need. `create_agent` stays
# direct because the MCP tool requires `task_ids` (purposeful for
# tool-driven agent creation), but the dashboard's "Create Agent"
# modal creates agents without preassigned tasks — going through the
# dispatcher would surface as a 400 instead of the current 200.
from ..tools.admin_tools import create_agent_tool_impl
import mcp.types as mcp_types # For handling the result from tool_impl

# Thin-adapter plumbing (Candidate C, 2026-06-02 architecture review).
# Mutating REST endpoints that have a 1:1 MCP-tool match dispatch
# through `dispatch_tool_call` so validation, auth, and audit logging
# live once — in the tool's inputSchema + @requires decorator + impl
# — rather than being re-implemented per surface.
from ..tools.registry import dispatch_tool_call, request_auth_token
from ..core.authorize import AuthRejected
from ..tools.registry import ToolInputValidationError


def _result_text(result: List[mcp_types.TextContent]) -> str:
    """Concatenate text blocks from a tool-call result."""
    if not result:
        return ""
    parts: List[str] = []
    for block in result:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


async def _dispatch_through_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    bearer_token: Optional[str],
    success_message: Optional[str] = None,
    extra_response: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """Run an MCP tool from a REST handler and translate the
    `list[TextContent]` result back into a dashboard-friendly JSON
    response.

    Auth: the dashboard sends the admin token in the JSON body, not as
    an Authorization header. We bind it on the `request_auth_token`
    ContextVar so `dispatch_tool_call`'s Q6e fallback injects it into
    `arguments.token` if not already there — same path an HTTP middleware
    would take.

    Error mapping (HTTP-shaped):
      * AuthRejected            → 403
      * ToolInputValidationError → 400
      * Tool result starting with "Error: ... not found" / "not found" → 404
      * Other tool-error text   → 400 (caller-error semantics)
      * Unexpected exception    → 500

    Success payload mirrors the legacy REST endpoints'
    ``{"success": true, "message": "...", ...extras}`` shape so the
    dashboard's ApiClient doesn't have to change.
    """
    cv_token = None
    if bearer_token:
        cv_token = request_auth_token.set(bearer_token)
    try:
        result = await dispatch_tool_call(tool_name, arguments)
    except AuthRejected as e:
        return JSONResponse(
            {"success": False, "error": e.reason, "message": e.reason},
            status_code=403,
        )
    except ToolInputValidationError as e:
        return JSONResponse(
            {"success": False, "error": str(e), "message": str(e)},
            status_code=400,
        )
    except Exception as e:
        logger.error(
            f"Unexpected error dispatching tool {tool_name!r}: {e}",
            exc_info=True,
        )
        return JSONResponse(
            {
                "success": False,
                "error": f"Tool dispatch failed: {e}",
                "message": f"Tool dispatch failed: {e}",
            },
            status_code=500,
        )
    finally:
        if cv_token is not None:
            request_auth_token.reset(cv_token)

    text = _result_text(result)
    # Tool impls report errors as plain-text "Error: ..." blocks. Map
    # those onto HTTP status codes so the dashboard sees the same shape
    # the legacy direct-DB handlers produced. We also catch the
    # without-"Error:"-prefix wording some tool impls use
    # (`terminate_agent` says "Agent 'x' not found or already
    # terminated."; `delete_project_context` says "None of the
    # specified keys exist..."). Treating any "not found" / "does not
    # exist" / "Cannot delete" sentence as a non-2xx keeps the
    # adapter's HTTP shape identical to the legacy REST endpoints.
    lower = text.lower().lstrip()
    is_error_prefix = (
        lower.startswith("error:") or lower.startswith("unauthorized")
    )
    is_not_found_phrase = (
        " not found" in lower
        or lower.startswith("not found")
        or "does not exist" in lower
        or "none of the specified keys exist" in lower
    )
    is_refusal_phrase = lower.startswith("cannot ")
    if is_error_prefix or is_not_found_phrase or is_refusal_phrase:
        status = 400
        if is_not_found_phrase:
            status = 404
        if "unauthorized" in lower:
            status = 403
        return JSONResponse(
            {"success": False, "error": text, "message": text},
            status_code=status,
        )

    payload: Dict[str, Any] = {
        "success": True,
        "message": success_message or text,
    }
    if extra_response:
        payload.update(extra_response)
    return JSONResponse(payload)


# --- Dashboard and API Endpoints ---

async def simple_status_api_route(request: Request) -> JSONResponse:
    # Handle OPTIONS for CORS preflight
    if request.method == 'OPTIONS':
        return await handle_options(request)
    
    try:
        # Get system status
        from ..db.actions.agent_db import get_all_active_agents_from_db
        from ..db.actions.task_db import get_all_tasks_from_db
        
        agents = get_all_active_agents_from_db()
        tasks = get_all_tasks_from_db()
        
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

async def graph_data_api_route(request: Request) -> JSONResponse:
    # // ... (implementation from previous response)
    try:
        data = await fetch_graph_data_logic(g.file_map.copy())
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving graph data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': str(e)}, status_code=500)

async def task_tree_data_api_route(request: Request) -> JSONResponse:
    # // ... (implementation from previous response)
    try:
        data = await fetch_task_tree_data_logic()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving task tree data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': str(e)}, status_code=500)

async def node_details_api_route(request: Request) -> JSONResponse:
    # // ... (implementation from previous response)
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
            row = cursor.fetchone();
            if row: details['data'] = dict(row)
            cursor.execute("SELECT timestamp, action_type, task_id, details FROM agent_actions WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT task_id, title, status, priority FROM tasks WHERE assigned_to = ? ORDER BY created_at DESC LIMIT 10", (actual_id_from_node,))
            details['related']['assigned_tasks'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'task':
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (actual_id_from_node,))
            row = cursor.fetchone();
            if row: details['data'] = dict(row)
            cursor.execute("SELECT timestamp, agent_id, action_type, details FROM agent_actions WHERE task_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'context':
            cursor.execute("SELECT * FROM project_context WHERE context_key = ?", (actual_id_from_node,))
            row = cursor.fetchone();
            if row: details['data'] = dict(row)
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
        if conn: conn.close()
    return JSONResponse(details)

async def agents_list_api_route(request: Request) -> JSONResponse:
    # GET /api/agents[?status=<status>]
    #
    # Without a `status` query param, returns every non-tombstone
    # agent row plus the synthetic "Admin" row used by the dashboard
    # graph (back-compat). Tombstone rows (status='tombstone',
    # agent_id like '[deleted-<original>]') are FK-target artefacts
    # of the purge cascade and never belong in user-facing output —
    # see all_data_api_route for the same filter rationale.
    #
    # With `status=<value>`, returns only agent rows whose status
    # matches exactly, EXCEPT `status=tombstone` which always
    # returns the empty list (tombstone is an internal DB state,
    # not an operator-queryable agent status). The synthetic Admin
    # row (status='system') is also filtered out under any
    # `status=<value>` filter — only rows whose status equals the
    # filter value survive. This shape replaces the router's
    # `list_agents` synthetic tool (Phase 7c, Q7.2 in plan).
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
            admin_style = get_node_style('admin')
            agents_list_data.append({
                'agent_id': 'Admin', 'status': 'system', 'color': admin_style.get('color', '#607D8B'),
                'created_at': 'N/A', 'current_task': 'N/A'
            })
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
        for row in cursor.fetchall(): agents_list_data.append(dict(row))
    except Exception as e:
        logger.error(f"Error fetching agents list: {e}", exc_info=True)
        return JSONResponse({'error': f'Failed to fetch agents list: {str(e)}'}, status_code=500)
    finally:
        if conn: conn.close()
    return JSONResponse(agents_list_data)

async def tokens_api_route(request: Request) -> JSONResponse:
    # // ... (implementation from previous response)
    try:
        # Guard: if the caller presents an Authorization: Bearer header
        # that resolves to a non-admin agent, refuse. This blocks the
        # worker→admin HTTP-side escalation path (issue O — sibling
        # of issue I via the REST surface).
        #
        # Unauthenticated callers still get the full response — that's
        # consistent with the dashboard-as-admin design (anyone reaching
        # the URL is implicitly admin; securing the URL is the
        # deployer's job).
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
            if bearer and not verify_token(bearer, "admin"):
                return JSONResponse(
                    {"error": "Unauthorized: admin token required"},
                    status_code=403,
                )

        agent_tokens_list = []
        for token, data in g.active_agents.items():
            if data.get("status") != "terminated":
                agent_tokens_list.append({"agent_id": data.get("agent_id"), "token": token})
        return JSONResponse({"admin_token": g.admin_token, "agent_tokens": agent_tokens_list})
    except Exception as e:
        logger.error(f"Error retrieving tokens for dashboard: {e}", exc_info=True)
        return JSONResponse({"error": f"Error retrieving tokens: {str(e)}"}, status_code=500)

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
    assigned_to_filter: Optional[str] = request.query_params.get('assigned_to')
    unassigned_raw = request.query_params.get('unassigned', '')
    unassigned_filter: bool = unassigned_raw.lower() in ('true', '1', 'yes')

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if unassigned_filter:
            cursor.execute(
                "SELECT * FROM tasks WHERE assigned_to IS NULL "
                "ORDER BY created_at DESC"
            )
        elif assigned_to_filter is not None:
            cursor.execute(
                "SELECT * FROM tasks WHERE assigned_to = ? "
                "ORDER BY created_at DESC",
                (assigned_to_filter,),
            )
        else:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        tasks_data = [dict(row) for row in cursor.fetchall()]
        return JSONResponse(tasks_data)
    except Exception as e:
        logger.error(f"Error fetching all tasks: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to fetch all tasks: {str(e)}"}, status_code=500)
    finally:
        if conn: conn.close()

async def update_task_details_api_route(request: Request) -> JSONResponse:
    # Dashboard task edit endpoint.
    #
    # Originally required (task_id, status) and used `status` as the
    # only mandatory field. The dashboard's Edit modal needs to mutate
    # individual fields independently (title-only, priority-only, …)
    # and to manage assignment, so the rules are:
    #   - task_id + admin token still required.
    #   - status is now OPTIONAL (status-only updates still supported).
    #   - At least one editable field must be supplied (otherwise the
    #     UPDATE is a no-op and 400 is clearer than success).
    #   - assigned_to: new field. <agent_id> assigns; null/empty
    #     string clears the assignment (NULL in DB).
    if request.method != 'POST': return JSONResponse({"error": "Method not allowed"}, status_code=405)
    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_auth_token = data.get('token'); task_id_to_update = data.get('task_id'); new_status = data.get('status')
        if not task_id_to_update:
            return JSONResponse({"error": "task_id is a required field."}, status_code=400)
        if not verify_token(admin_auth_token, required_role='admin'): return JSONResponse({"error": "Invalid admin token"}, status_code=403)
        # The set of recognised editable fields. At least one must be
        # supplied; otherwise the request is a no-op and rejected.
        EDITABLE_KEYS = {"status", "title", "description", "priority", "notes", "assigned_to"}
        supplied_editable = [k for k in EDITABLE_KEYS if k in data]
        if not supplied_editable:
            return JSONResponse(
                {"error": "at least one editable field is required (status, title, description, priority, notes, assigned_to)."},
                status_code=400,
            )
        requesting_admin_id = auth_get_agent_id(admin_auth_token)
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT notes FROM tasks WHERE task_id = ?", (task_id_to_update,)); task_row = cursor.fetchone()
        if not task_row: return JSONResponse({"error": "Task not found"}, status_code=404)
        existing_notes_str = task_row["notes"]
        update_fields: List[str] = []; params: List[Any] = []; log_details: Dict[str, Any] = {}
        if new_status:
            update_fields.append("status = ?"); params.append(new_status)
            log_details["status_updated_to"] = new_status
        update_fields.append("updated_at = ?"); params.append(datetime.datetime.now().isoformat())
        if 'title' in data and data['title'] is not None: update_fields.append("title = ?"); params.append(data['title']); log_details["title_changed"] = True
        if 'description' in data and data['description'] is not None: update_fields.append("description = ?"); params.append(data['description']); log_details["description_changed"] = True
        if 'priority' in data and data['priority']: update_fields.append("priority = ?"); params.append(data['priority']); log_details["priority_changed"] = True
        if 'assigned_to' in data:
            # null / '' / 'unassigned' clears the assignment; any other
            # value is stored verbatim as the agent_id.
            raw_assigned = data['assigned_to']
            new_assigned: Optional[str]
            if raw_assigned is None or (isinstance(raw_assigned, str) and raw_assigned.strip() in ('', 'unassigned')):
                new_assigned = None
            else:
                new_assigned = str(raw_assigned).strip()
            update_fields.append("assigned_to = ?"); params.append(new_assigned)
            log_details["assigned_to_changed"] = new_assigned
        if 'notes' in data and data['notes'] and isinstance(data['notes'], str) and data['notes'].strip():
            try: current_notes_list = json.loads(existing_notes_str or "[]")
            except json.JSONDecodeError: current_notes_list = []
            new_note_entry = {"timestamp": datetime.datetime.now().isoformat(), "author": requesting_admin_id, "content": data['notes'].strip()}
            current_notes_list.append(new_note_entry); update_fields.append("notes = ?"); params.append(json.dumps(current_notes_list)); log_details["notes_added"] = True
        params.append(task_id_to_update)
        if update_fields:
            placeholders = ', '.join(update_fields)
            query = f"UPDATE tasks SET {placeholders} WHERE task_id = ?"
            cursor.execute(query, tuple(params))
        log_agent_action_to_db(cursor, requesting_admin_id, "updated_task_dashboard", task_id=task_id_to_update, details=log_details); conn.commit()
        if task_id_to_update in g.tasks:
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id_to_update,)); updated_task_for_cache = cursor.fetchone()
            if updated_task_for_cache:
                g.tasks[task_id_to_update] = dict(updated_task_for_cache)
                for field_key in ["child_tasks", "depends_on_tasks", "notes"]:
                    if isinstance(g.tasks[task_id_to_update].get(field_key), str):
                        try: g.tasks[task_id_to_update][field_key] = json.loads(g.tasks[task_id_to_update][field_key] or "[]")
                        except json.JSONDecodeError: g.tasks[task_id_to_update][field_key] = []
            else: del g.tasks[task_id_to_update]
        return JSONResponse({"success": True, "message": "Task updated successfully via dashboard."})
    except ValueError as e_val: return JSONResponse({"error": str(e_val)}, status_code=400)    
    except sqlite3.Error as e_sql:
        if conn: conn.rollback();
        logger.error(f"DB error updating task via dashboard: {e_sql}", exc_info=True)
        return JSONResponse({"error": f"Failed to update task (DB): {str(e_sql)}"}, status_code=500)
    except Exception as e:
        if conn: conn.rollback();
        logger.error(f"Error updating task via dashboard: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to update task: {str(e)}"}, status_code=500)
    finally:
        if conn: conn.close()


# --- ADDED: Dashboard-specific Agent Management API Endpoints ---

# Original: main.py lines 2022-2058 (create_agent_api function)
async def create_agent_dashboard_api_route(request: Request) -> JSONResponse:
    """Dashboard API endpoint to create an agent. Calls the admin tool internally."""
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await get_sanitized_json_body(request)
        admin_auth_token = data.get("token")
        agent_id = data.get("agent_id")
        capabilities = data.get("capabilities", []) # Optional
        working_directory = data.get("working_directory") # Optional

        # This endpoint itself requires admin authentication
        if not verify_token(admin_auth_token, "admin"):
            return JSONResponse({"message": "Unauthorized: Invalid admin token for API call"}, status_code=401)

        if not agent_id:
            return JSONResponse({"message": "Agent ID is required"}, status_code=400)

        # Forbid `[` and `]` in agent_id — reserved for the purge
        # cascade tombstone format `[deleted-<id>]`. Defense-in-depth:
        # the underlying tool rejects too, but doing it here lets us
        # respond with a clean 400 instead of bubbling a tool error.
        if "[" in agent_id or "]" in agent_id:
            return JSONResponse(
                {"message": f"Invalid agent_id {agent_id!r}: `[` and `]` "
                            "are reserved (purge-cascade tombstone format)."},
                status_code=400,
            )

        # Prepare arguments for the create_agent_tool_impl
        tool_args = {
            "token": admin_auth_token, # The tool_impl will verify this again
            "agent_id": agent_id,
            "capabilities": capabilities,
            "working_directory": working_directory
        }
        
        # Call the already refactored tool implementation
        result_list: List[mcp_types.TextContent] = await create_agent_tool_impl(tool_args)
        
        # Process the result from tool_impl to form a JSONResponse
        # The tool_impl returns a list of TextContent objects.
        # The original API returned a simple JSON message.
        if result_list and result_list[0].text.startswith(f"Agent '{agent_id}' created successfully."):
            # Extract token if possible for dashboard convenience (original API did this)
            # This is a bit fragile as it relies on string parsing of the tool's output.
            agent_token_from_result = None
            for line in result_list[0].text.split('\n'):
                if line.startswith("Token: "):
                    agent_token_from_result = line.split("Token: ", 1)[1]
                    break
            return JSONResponse({
                "message": f"Agent '{agent_id}' created successfully via dashboard API.",
                "agent_token": agent_token_from_result # May be None if not parsed
            })
        else:
            # Return the error message from the tool
            error_message = result_list[0].text if result_list else "Unknown error creating agent."
            # Determine appropriate status code based on error message
            status_code = 400 # Default bad request
            if "Unauthorized" in error_message: status_code = 401
            if "already exists" in error_message: status_code = 409 # Conflict
            return JSONResponse({"message": error_message}, status_code=status_code)

    except ValueError as e_val: # From get_sanitized_json_body
        return JSONResponse({"message": str(e_val)}, status_code=400)
    except Exception as e:
        logger.error(f"Error in create_agent_dashboard_api_route: {e}", exc_info=True)
        return JSONResponse({"message": f"Error creating agent via dashboard API: {str(e)}"}, status_code=500)

# Thin adapter (Candidate C, 2026-06-02 architecture review): dispatch
# through the `terminate_agent` MCP tool so validation +
# auth-rejection wording cannot drift between the dashboard surface
# and the MCP surface. Wire-shape parity is pinned by
# tests/test_rest_mcp_tool_parity.py.
async def terminate_agent_dashboard_api_route(request: Request) -> JSONResponse:
    """POST /api/terminate-agent — admin terminates an agent.

    Thin adapter over the ``terminate_agent`` MCP tool. The tool's
    ``inputSchema`` enforces ``agent_id`` required + admin-only role
    (via ``@requires("admin")``); this handler just translates the
    HTTP request → tool call → JSON response.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e_val:
        return JSONResponse({"message": str(e_val)}, status_code=400)

    admin_token = data.get("token")
    agent_id = data.get("agent_id")
    return await _dispatch_through_tool(
        "terminate_agent",
        {"agent_id": agent_id} if agent_id else {},
        bearer_token=admin_token,
        success_message=(
            f"Agent '{agent_id}' terminated successfully via dashboard API."
            if agent_id else None
        ),
    )


# --- Agent restore + purge endpoints (this PR) ---
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


async def restore_agent_api_route(request: Request) -> JSONResponse:
    """POST /api/agents/<id>/restore — admin reverses a soft-delete.

    Side effects of the original terminate (cleared current_task,
    released held files, killed tmux session) are NOT undone. Admin
    reassigns work explicitly. We only flip status back and re-add to
    g.active_agents so the dashboard's active-list/token-list pick it
    up again.
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
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse(
                {"error": "Unauthorized: admin token required"},
                status_code=403,
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
        if row["status"] != "terminated":
            return JSONResponse(
                {"error": f"Agent '{agent_id}' is not terminated "
                          f"(status={row['status']!r}); nothing to restore"},
                status_code=409,
            )

        agent_token = row["token"]
        now = datetime.datetime.now().isoformat()
        cursor.execute(
            "UPDATE agents SET status = ?, terminated_at = NULL, "
            "updated_at = ? WHERE agent_id = ?",
            ("created", now, agent_id),
        )
        log_agent_action_to_db(
            cursor, "admin", "restored_agent",
            details={"agent_id": agent_id},
        )
        conn.commit()

        # Re-add to in-memory active map so the dashboard sees them.
        # We rebuild the entry from DB-known fields; capabilities/color
        # are not surfaced through this re-add path (admin can fetch
        # via /api/all-data if needed).
        cursor.execute(
            "SELECT agent_id, capabilities, created_at, status, color "
            "FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        full = cursor.fetchone()
        if full is not None:
            try:
                caps = json.loads(full["capabilities"] or "[]")
            except (TypeError, json.JSONDecodeError):
                caps = []
            g.active_agents[agent_token] = {
                "agent_id": full["agent_id"],
                "capabilities": caps,
                "created_at": full["created_at"],
                "status": full["status"],
                "color": full["color"],
            }

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
        return JSONResponse(
            {"error": f"Failed to restore agent: {str(e)}"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()


async def edit_agent_api_route(request: Request) -> JSONResponse:
    """POST /api/agents/<id>/edit — admin updates mutable agent fields.

    Accepts an admin token in the JSON body alongside any combination of
    the editable fields: `capabilities` (list[str]), `color` (str),
    `working_directory` (str). Returns 400 if none of the editable
    fields are supplied (avoids no-op writes), 404 if the agent does
    not exist.

    Non-whitelisted fields in the body are silently ignored — the
    endpoint never touches status/agent_id/token; those have their own
    dedicated flows (terminate/restore/purge for status; create for
    agent_id+token; nothing for editing tokens).
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
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse(
                {"error": "Unauthorized: admin token required"},
                status_code=403,
            )

        # Whitelisted editable fields. Anything else in `data` is ignored
        # (defense in depth — status / agent_id / token must not flow
        # through this endpoint).
        #
        # Event-coord PR-1: `auto_event_loop` (per-agent wake-loop
        # toggle) joins the editable list — dashboard's agent-edit
        # modal flips it to opt this agent out of the wake-loop
        # bootstrap shipped in PR-2.
        editable = (
            'capabilities', 'color', 'working_directory', 'aoe_session_id',
            'auto_event_loop',
        )
        updates = {k: data[k] for k in editable if k in data}
        if not updates:
            return JSONResponse(
                {"error": "No editable fields supplied. Accepts any of: "
                          + ", ".join(editable)},
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

        # Use the existing helper so JSON-serialisation of capabilities
        # + updated_at bookkeeping stays consistent.
        from agent_mcp.db.actions.agent_db import update_agent_db_field

        applied: Dict[str, Any] = {}
        for field, value in updates.items():
            ok = update_agent_db_field(agent_id, field, value)
            if not ok:
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
            cursor, "admin", "edited_agent",
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
        return JSONResponse(
            {"error": f"Failed to edit agent: {str(e)}"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()


def _gather_purge_preview(cursor, agent_id: str) -> Dict[str, Any]:
    """Compute the blast-radius counts + samples for a future purge."""
    cursor.execute(
        "SELECT COUNT(*) AS n FROM agent_messages WHERE sender_id = ?",
        (agent_id,),
    )
    messages_sent = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT COUNT(*) AS n FROM agent_messages WHERE recipient_id = ?",
        (agent_id,),
    )
    messages_received = cursor.fetchone()["n"]
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

    cursor.execute(
        "SELECT message_content, timestamp FROM agent_messages "
        "WHERE sender_id = ? ORDER BY timestamp DESC LIMIT 3",
        (agent_id,),
    )
    sample_messages_sent = [
        {"content": _trim(r["message_content"]), "timestamp": r["timestamp"]}
        for r in cursor.fetchall()
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


async def purge_preview_api_route(request: Request) -> JSONResponse:
    """GET /api/agents/<id>/purge-preview — blast-radius counts + samples.

    Admin-only. Accepts the admin token via query parameter (so a plain
    GET works without a body, which browsers strip per the Fetch spec).
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'GET':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    agent_id = request.path_params.get('agent_id')
    if not agent_id:
        return JSONResponse({"error": "agent_id required"}, status_code=400)

    admin_token = request.query_params.get('token')
    if not verify_token(admin_token, required_role='admin'):
        return JSONResponse(
            {"error": "Unauthorized: admin token required"},
            status_code=403,
        )

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
        return JSONResponse(
            {"error": f"Failed to compute purge preview: {str(e)}"},
            status_code=500,
        )
    finally:
        if conn:
            conn.close()


async def purge_agent_api_route(request: Request) -> JSONResponse:
    """DELETE /api/agents/<id>?cascade=true — hard delete + cascade tombstone.

    Admin-only. Wraps the cascade in a transaction (BEGIN/COMMIT) so a
    half-purged state is impossible if any step fails. The DELETE on
    agents runs LAST so logical references can be tombstoned while the
    row is still present (no DB foreign keys, but this preserves
    intent-readability).

    Refuses without ?cascade=true so a bare DELETE doesn't silently
    hard-delete data.
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
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse(
                {"error": "Unauthorized: admin token required"},
                status_code=403,
            )

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
            cursor.execute(
                "INSERT OR IGNORE INTO agents "
                "(token, agent_id, capabilities, created_at, status, "
                " working_directory, color, updated_at) "
                "VALUES (?, ?, '[]', ?, 'tombstone', '', '#000000', ?)",
                (
                    f"__tombstone_{agent_id}",
                    tombstone,
                    datetime.datetime.now().isoformat(),
                    datetime.datetime.now().isoformat(),
                ),
            )
            cursor.execute(
                "UPDATE agent_messages SET sender_id = ? WHERE sender_id = ?",
                (tombstone, agent_id),
            )
            cursor.execute(
                "UPDATE agent_messages SET recipient_id = ? "
                "WHERE recipient_id = ?",
                (tombstone, agent_id),
            )
            cursor.execute(
                "UPDATE tasks SET created_by = ? WHERE created_by = ?",
                (tombstone, agent_id),
            )
            # Reassignment: anything assigned to this agent becomes
            # unassigned (admin can pick it up + reassign).
            cursor.execute(
                "UPDATE tasks SET assigned_to = NULL, status = 'unassigned' "
                "WHERE assigned_to = ?",
                (agent_id,),
            )
            cursor.execute(
                "UPDATE agent_actions SET agent_id = ? WHERE agent_id = ?",
                (tombstone, agent_id),
            )
            # Audit the purge itself — written *before* the agent row
            # disappears so the action log has a non-tombstoned
            # 'purged_agent' entry attributable to admin.
            log_agent_action_to_db(
                cursor, "admin", "purged_agent",
                details={
                    "agent_id": agent_id,
                    "tombstone": tombstone,
                    "counts": counts,
                },
            )
            # DELETE the agents row LAST.
            cursor.execute("DELETE FROM agents WHERE agent_id = ?",
                           (agent_id,))
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
        return JSONResponse(
            {"error": f"Failed to purge agent: {str(e)}"}, status_code=500,
        )
    finally:
        if conn:
            conn.close()


# --- Comprehensive Data Endpoint ---
# Default per-section LIMIT for /api/all-data; bounded by the 2026-06-02
# database review (item 2) so a project with thousands of rows no longer
# materialises an unbounded payload on every dashboard refresh. Callers
# that want more can pass `?limit=N`, but `_ALL_DATA_MAX_LIMIT` clamps
# the upper bound to keep the JSON shape sane.
_ALL_DATA_DEFAULT_LIMIT = 500
_ALL_DATA_MAX_LIMIT = 5000


async def all_data_api_route(request: Request) -> JSONResponse:
    """Get all data in one call for caching on frontend"""
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
            # Skip the synthetic 'admin' pseudo-agent row (seeded by
            # migration 0008 / `_ensure_admin_pseudo_agent_row` so the
            # post-PR #100 FK constraints have a target). The row stays
            # in the DB — it's still load-bearing for FKs — but we
            # don't surface it in the agents list, otherwise it shows
            # up alongside the hardcoded 'Admin' display entry inserted
            # below and the dashboard renders two Admin rows.
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
            # Event-coord PR-3: surface whether this agent currently
            # has an in-flight `wait_for_events` long-poll call. PR-2
            # serialises wait_for_events via `g.lock_for(agent_id)`
            # (one-call-per-agent enforcement). The locked() snapshot
            # is what powers the dashboard "waiting" chip + the
            # Settings page "X agents currently in wait" count. We
            # don't lazily create the lock here — only agents that
            # have ever entered `wait_for_events` have a lock object
            # in the registry; the absence means definitively FALSE.
            existing_lock = g.agent_event_locks.get(
                agent_dict['agent_id']
            )
            agent_dict['wait_for_events_in_flight'] = bool(
                existing_lock is not None and existing_lock.locked()
            )
            agents_data.append(agent_dict)

        # Add admin as special agent. The display label stays 'Admin'
        # (capital A) because the entire dashboard frontend keys off
        # `agent_id === 'Admin'` for special-case handling (no edit /
        # terminate buttons, admin-token mapping, "always show" filter).
        # The underlying DB row is `agent_id='admin'` lowercase — kept
        # separate from this UI entry on purpose.
        agents_data.insert(0, {
            'agent_id': 'Admin',
            'status': 'system',
            'auth_token': g.admin_token,
            'created_at': 'N/A',
            'current_task': 'N/A',
            # Admin never enters the wake loop — always FALSE so the
            # dashboard renders a uniform shape for every row.
            'wait_for_events_in_flight': False,
        })

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
            "admin_token": g.admin_token,
            "timestamp": datetime.datetime.now().isoformat()
        }
        
        return JSONResponse(
            response_data,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type'
            }
        )
        
    except Exception as e:
        logger.error(f"Error fetching all data: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to fetch all data: {str(e)}"}, status_code=500)
    finally:
        if conn:
            conn.close()

async def context_data_api_route(request: Request) -> JSONResponse:
    """Get only context data"""
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

        return JSONResponse(
            context_data,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type'
            }
        )

    except Exception as e:
        logger.error(f"Error fetching context data: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to fetch context data: {str(e)}"}, status_code=500)

# --- CORS Preflight Handler ---
async def handle_options(request: Request) -> Response:
    """Handle OPTIONS requests for CORS preflight"""
    return PlainTextResponse(
        '',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Max-Age': '86400',
        }
    )

async def aoe_health_api_route(request: Request) -> JSONResponse:
    """GET /api/aoe/health — admin-only AoE-reachability probe.

    Pings the configured AoE instance with the current bearer token
    (resolved live, including file-sourced rotations) and reports back:

      {"status": "ok",            "session_count": N, "base_url": "..."}
      {"status": "disabled",      "message": "config_aoe_notify_enabled is off"}
      {"status": "unauthorized",  "message": "AoE returned 401 ..."}
      {"status": "unreachable",   "message": "..."}
      {"status": "misconfigured", "message": "no bearer token resolved"}

    Used by the Settings tab to surface a "your AoE token has gone
    stale" warning without requiring the admin to attempt a real send.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'GET':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Auth: admin only. Accept token via Authorization: Bearer header
    # OR ?token=<>.
    auth_header = request.headers.get("Authorization", "")
    token: Optional[str] = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip() or None
    if token is None:
        token = request.query_params.get("token")
    if not token or not verify_token(token, "admin"):
        return JSONResponse(
            {"error": "Unauthorized: admin token required"},
            status_code=401,
        )

    from agent_mcp.features.aoe_notify import check_health

    try:
        result = await check_health()
    except Exception as e:
        logger.error("AoE health probe crashed: %s", e, exc_info=True)
        return JSONResponse(
            {"status": "unreachable", "message": f"probe crashed: {e}"},
            status_code=200,
        )
    return JSONResponse(result)


# --- Prompt Book catalog (plan Phase 6) ---
async def prompts_catalog_api_route(request):
    """`GET /api/prompts/catalog` — the single source of truth
    for the Prompt Book catalogue.

    Sourced from `agent_mcp/prompts/catalog.json` so MCP
    `prompts/list` and the dashboard read the same data.
    """
    from ..prompts import load_catalog
    return JSONResponse(load_catalog())


# --- Route Definitions List ---
routes = [
    Route('/api/prompts/catalog', endpoint=prompts_catalog_api_route, name="prompts_catalog_api", methods=['GET', 'OPTIONS']),
    Route('/api/aoe/health', endpoint=aoe_health_api_route, name="aoe_health_api", methods=['GET', 'OPTIONS']),
    Route('/api/all-data', endpoint=all_data_api_route, name="all_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/status', endpoint=simple_status_api_route, name="simple_status_api", methods=['GET', 'OPTIONS']),
    Route('/api/graph-data', endpoint=graph_data_api_route, name="graph_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/task-tree-data', endpoint=task_tree_data_api_route, name="task_tree_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/node-details', endpoint=node_details_api_route, name="node_details_api", methods=['GET', 'OPTIONS']),
    Route('/api/agents', endpoint=agents_list_api_route, name="agents_list_api", methods=['GET', 'OPTIONS']),
    # Modern POST shape — mirrors /api/agents/<id>/restore, /edit,
    # /purge-preview, etc. The dashboard's apiClient.createAgent has
    # always called this URL; pre-fix it 405'd because only GET was
    # registered (Deploy button was silently broken since the
    # dashboard was introduced). The handler is the same one the
    # back-compat /api/create-agent alias below routes to.
    Route('/api/agents', endpoint=create_agent_dashboard_api_route, name="create_agent_api", methods=['POST']),
    Route('/api/tokens', endpoint=tokens_api_route, name="tokens_api", methods=['GET', 'OPTIONS']),
    Route('/api/tasks', endpoint=all_tasks_api_route, name="all_tasks_api", methods=['GET', 'OPTIONS']),
    Route('/api/update-task-dashboard', endpoint=update_task_details_api_route, name="update_task_dashboard_api", methods=['POST', 'OPTIONS']),
    
    # Added back for 1-to-1 dashboard compatibility
    Route('/api/create-agent', endpoint=create_agent_dashboard_api_route, name="create_agent_dashboard_api", methods=['POST', 'OPTIONS']),
    Route('/api/terminate-agent', endpoint=terminate_agent_dashboard_api_route, name="terminate_agent_dashboard_api", methods=['POST', 'OPTIONS']),

    # Restore + Purge (this PR). Path-style routes so the agent_id is
    # part of the URL — matches the dashboard's `/agents/<id>/...` shape.
    Route('/api/agents/{agent_id}/restore', endpoint=restore_agent_api_route, name="restore_agent_api", methods=['POST', 'OPTIONS']),
    Route('/api/agents/{agent_id}/edit', endpoint=edit_agent_api_route, name="edit_agent_api", methods=['POST', 'OPTIONS']),
    Route('/api/agents/{agent_id}/purge-preview', endpoint=purge_preview_api_route, name="purge_preview_api", methods=['GET', 'OPTIONS']),
    Route('/api/agents/{agent_id}', endpoint=purge_agent_api_route, name="purge_agent_api", methods=['DELETE', 'OPTIONS']),

    # Catch-all OPTIONS handler for any API route
    Route('/api/{path:path}', endpoint=handle_options, methods=['OPTIONS']),
]

# --- Test/Demo Data Endpoint ---
async def create_sample_memories_route(request: Request) -> JSONResponse:
    """Create sample memory entries for testing"""
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
                        created_by=memory['updated_by'],
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

# Memory CRUD API endpoints
async def create_memory_api_route(request: Request) -> JSONResponse:
    """Create a new memory entry"""
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    session = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        context_key = data.get('context_key')
        context_value = data.get('context_value')
        description = data.get('description')

        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        if not context_key:
            return JSONResponse({"error": "context_key is required"}, status_code=400)

        requesting_admin_id = auth_get_agent_id(admin_token)

        session = SessionLocal()

        # Check if key already exists
        existing = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == context_key)
            .one_or_none()
        )
        if existing is not None:
            return JSONResponse({"error": "Memory with this key already exists"}, status_code=409)

        current_time = datetime.datetime.now().isoformat()

        session.add(
            ProjectContext(
                context_key=context_key,
                value=json.dumps(context_value),
                created_at=current_time,
                created_by=requesting_admin_id,
                updated_at=current_time,
                updated_by=requesting_admin_id,
                description=description,
            )
        )
        session.flush()

        # Log the action via the session's raw connection so it lands
        # in the same transaction as the project_context insert.
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(cursor, requesting_admin_id, "created_memory", details={"context_key": context_key})
        session.commit()

        # PR-2 event-coord: creating the global toggle key with a
        # falsy initial value should wake in-flight wait_for_events
        # so they re-evaluate (same shape as the update path).
        if context_key == "config_auto_event_loop_global":
            try:
                g.wake_all_for_flag_recheck()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "wake_all_for_flag_recheck failed after global "
                    "toggle create: %s", e,
                )

        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' created successfully"
        })

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if session is not None:
            session.rollback()
        logger.error(f"Error creating memory: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to create memory: {str(e)}"}, status_code=500)
    finally:
        if session is not None:
            session.close()

async def update_memory_api_route(request: Request) -> JSONResponse:
    """Update an existing memory entry"""
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'PUT':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Extract context_key from URL path
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "context_key is required in URL"}, status_code=400)

    context_key = path_parts[-1]

    session = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        context_value = data.get('context_value')
        description = data.get('description')

        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        requesting_admin_id = auth_get_agent_id(admin_token)

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

        # PR-2 event-coord: a flip of the global wake-loop toggle must
        # wake every in-flight wait_for_events so they can re-evaluate
        # and return `stop_listening` if the new state is OFF.
        if context_key == "config_auto_event_loop_global":
            try:
                g.wake_all_for_flag_recheck()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "wake_all_for_flag_recheck failed after global "
                    "toggle update: %s", e,
                )

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
        return JSONResponse({"error": f"Failed to update memory: {str(e)}"}, status_code=500)
    finally:
        if session is not None:
            session.close()

async def delete_memory_api_route(request: Request) -> JSONResponse:
    """DELETE /api/memories/<context_key> — admin deletes a memory.

    Thin adapter over the ``delete_project_context`` MCP tool
    (Candidate C, 2026-06-02 architecture review). The tool's
    ``inputSchema`` requires ``context_key`` (or ``context_keys``),
    auth is gated by the tool's own per-key creator-ownership matrix
    (``@requires("any")``; admins pass through unconditionally).

    The MCP tool refuses to delete "critical" keys (``config_*``,
    ``server_*``, ``mcp_*``, ``database_*``, ``system_*``) without
    ``force_delete=true``. The legacy REST handler had no such guard,
    so we pass ``force_delete=true`` to preserve wire compatibility —
    the dashboard never sent this flag and would otherwise start
    seeing 400s on system keys it could delete before. Wire-shape
    parity is pinned by tests/test_rest_mcp_tool_parity.py.
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

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    admin_token = data.get('token')
    return await _dispatch_through_tool(
        "delete_project_context",
        {"context_key": context_key, "force_delete": True},
        bearer_token=admin_token,
        success_message=f"Memory '{context_key}' deleted successfully",
    )

# --- Task CRUD endpoints (UPSTREAM_ISSUES.md issue C) ---
# Tasks already have GET /api/tasks (list) and POST /api/update-task-dashboard
# (mutate). The dashboard's "Create Task" and per-row delete buttons need
# POST /api/tasks and DELETE /api/tasks/<id>; without these the dashboard
# routes through a per-request MCP-tools/call bridge in reverse-proxy
# deployments, which is heavyweight and admin-bypassing.

import uuid as _uuid


async def create_task_api_route(request: Request) -> JSONResponse:
    """Create a new task. Admin token in JSON body (Q6a.1 convention).

    Body: {"token", "task_title", "task_description", "priority"?,
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
        admin_token = data.get('token')
        title = data.get('task_title')
        description = data.get('task_description', '')
        priority = data.get('priority', 'medium')
        assigned_to = data.get('assigned_to')  # nullable
        parent_task = data.get('parent_task')  # nullable
        # Event-coord PR-1: optional capability gate (list of free-text
        # labels, normalized to lowercase+stripped+deduped at write
        # time via the shared helper). Empty/missing => stored as NULL
        # ("anyone can claim", matches broadcast semantics).
        from agent_mcp.utils.capability_normalization import normalize_capabilities

        _norm_caps = normalize_capabilities(data.get('required_capabilities'))
        required_caps_json = json.dumps(_norm_caps) if _norm_caps else None

        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        if not title:
            return JSONResponse(
                {"error": "task_title is required"}, status_code=400
            )

        requesting_admin_id = auth_get_agent_id(admin_token)
        task_id = f"task_{_uuid.uuid4().hex[:12]}"
        now = datetime.datetime.now().isoformat()
        status = 'pending' if assigned_to else 'unassigned'

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (
                task_id, title, description, assigned_to, created_by,
                status, priority, created_at, updated_at,
                parent_task, child_tasks, depends_on_tasks, notes,
                required_capabilities
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, title, description, assigned_to, requesting_admin_id,
                status, priority, now, now,
                parent_task, '[]', '[]', '[]',
                required_caps_json,
            ),
        )
        log_agent_action_to_db(
            cursor, requesting_admin_id, "created_task",
            task_id=task_id, details={"title": title, "assigned_to": assigned_to},
        )
        conn.commit()

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
        return JSONResponse(
            {"error": f"Failed to create task: {str(e)}"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


async def delete_task_api_route(request: Request) -> JSONResponse:
    """DELETE /api/tasks/<task_id> — admin deletes a task.

    Thin adapter over the ``delete_task`` MCP tool (Candidate C,
    2026-06-02 architecture review). Validation
    (``task_id`` required) and admin-only auth live in the tool's
    ``inputSchema`` + ``@requires("admin")``. Cascade safety (children
    / dependents) is handled by the tool impl. Wire-shape parity is
    pinned by tests/test_rest_mcp_tool_parity.py.
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
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    admin_token = data.get('token')
    # ``force_delete=True`` matches the legacy REST behavior (the
    # direct-DB handler had no cascade safety check). The MCP tool's
    # default is False; passing True here preserves wire compatibility
    # — the dashboard never sent force_delete, and silently failing on
    # tasks with children would break existing flows.
    return await _dispatch_through_tool(
        "delete_task",
        {"task_id": task_id, "force_delete": True},
        bearer_token=admin_token,
        success_message=f"Task '{task_id}' deleted successfully",
    )


# --- Messages CRUD endpoints (Phase 6 PR #20 / issue P) ---
# agent_messages accumulates indefinitely; reading via get_agent_messages
# marks `read=1` but never deletes. The dashboard's new Messages tab
# needs list+filter+compose+mark-read access. Admin-only.


_MESSAGE_TYPES = ("text", "system", "notification", "task_update",
                  "assistance_request", "stop_command")
_MESSAGE_PRIORITIES = ("low", "normal", "high", "urgent")


async def list_messages_api_route(request: Request) -> JSONResponse:
    """POST /api/messages/query with rich filters (admin token in JSON body).

    Originally exposed as GET, but browsers strip request bodies from
    GET (per the Fetch spec), which broke the dashboard's Messages tab.
    We use POST + a dedicated /query suffix so that compose
    (POST /api/messages) and listing (POST /api/messages/query) coexist
    without method overloading.

    Body fields:
      token          (required, admin)
      from           sender_id filter
      to             recipient_id filter
      between        [a, b] — messages either direction between two agents
      type           message_type filter
      priority       priority filter
      read           bool — read flag filter
      since/until    ISO timestamp window
      q              content substring (LIKE %q%)
      limit/offset   pagination (default 50 / 0)
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        filter_from = data.get('from')
        filter_to = data.get('to')
        filter_between = data.get('between')  # list of two ids
        filter_type = data.get('type')
        filter_priority = data.get('priority')
        filter_read = data.get('read')  # bool
        filter_since = data.get('since')
        filter_until = data.get('until')
        filter_q = data.get('q')
        limit = int(data.get('limit', 50))
        offset = int(data.get('offset', 0))

        if limit < 1 or limit > 500:
            return JSONResponse(
                {"error": "limit must be 1..500"}, status_code=400
            )

        where = []
        params: list = []
        if filter_from is not None:
            where.append("sender_id = ?")
            params.append(filter_from)
        if filter_to is not None:
            where.append("recipient_id = ?")
            params.append(filter_to)
        if (
            isinstance(filter_between, list)
            and len(filter_between) == 2
            and all(isinstance(x, str) for x in filter_between)
        ):
            a, b = filter_between
            where.append("((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))")
            params.extend([a, b, b, a])
        if filter_type is not None:
            where.append("message_type = ?")
            params.append(filter_type)
        if filter_priority is not None:
            where.append("priority = ?")
            params.append(filter_priority)
        if filter_read is not None:
            where.append("read = ?")
            params.append(1 if filter_read else 0)
        if filter_since is not None:
            where.append("timestamp >= ?")
            params.append(filter_since)
        if filter_until is not None:
            where.append("timestamp <= ?")
            params.append(filter_until)
        if filter_q:
            where.append("message_content LIKE ?")
            params.append(f"%{filter_q}%")

        sql = "SELECT * FROM agent_messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]

        # total count for pagination UIs
        count_sql = "SELECT COUNT(*) AS n FROM agent_messages"
        if where:
            count_sql += " WHERE " + " AND ".join(where)
        cursor.execute(count_sql, params[:-2] if where else [])
        total = cursor.fetchone()["n"]

        return JSONResponse({"messages": rows, "total": total,
                             "limit": limit, "offset": offset})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error listing messages: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to list messages: {str(e)}"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


async def list_participants_api_route(request: Request) -> JSONResponse:
    """POST /api/messages/participants — agents available as filter values.

    Returns the set of agent identifiers that should populate the
    Messages tab's From/To filter dropdowns. /api/agents was the
    previous source but returns every row including
    ``status='terminated'``, leaking ghost agents that no longer appear
    on the Agents page.

    Response shape::

        {
          "live": [{"agent_id": "...", "status": "..."}, ...],
          "tombstones": ["[deleted-old-worker-1]", ...]
        }

    ``live`` excludes terminated agents and prepends a synthetic
    ``admin`` entry (the agents table has no admin row, but admin is a
    valid sender/recipient).

    ``tombstones`` are DISTINCT sender_id / recipient_id values that
    begin with ``[deleted-`` — the marker the PR C agent-purge cascade
    writes when an agent is permanently removed. Sorted lexicographically
    so the dropdown order is stable. Empty list until PR C lands.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        conn = get_db_connection()
        cursor = conn.cursor()

        # Live agents: anything that hasn't been terminated AND isn't a
        # tombstone row. We keep 'pending'/'failed'/etc. visible so
        # historical messages from those agents stay filterable. The
        # 'tombstone' state is a DB-internal FK target written by
        # _purge_tombstone (same file) — it must never surface in the
        # participants dropdown (live-verified leak on washing-brothers
        # 2026-06-06: 6 [deleted-*] tombstones in /live).
        cursor.execute(
            "SELECT agent_id, status FROM agents "
            "WHERE status IS NULL OR status NOT IN ('terminated', 'tombstone') "
            "ORDER BY agent_id ASC"
        )
        live = [dict(row) for row in cursor.fetchall()]

        # Always-present admin participant. Prepended so it sorts to the
        # top of the dropdown regardless of agent_id ordering.
        if not any(a.get('agent_id', '').lower() == 'admin' for a in live):
            live.insert(0, {"agent_id": "admin", "status": "system"})

        # Tombstones: DISTINCT sender_id UNION recipient_id values
        # beginning with the literal '[deleted-' marker. UNION
        # deduplicates across the two columns.
        cursor.execute(
            "SELECT sender_id AS id FROM agent_messages "
            "WHERE sender_id LIKE '[deleted-%' "
            "UNION "
            "SELECT recipient_id AS id FROM agent_messages "
            "WHERE recipient_id LIKE '[deleted-%' "
            "ORDER BY id ASC"
        )
        tombstones = [row["id"] for row in cursor.fetchall()]

        return JSONResponse({"live": live, "tombstones": tombstones})
    except Exception as e:
        logger.error(f"Error listing participants: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to list participants: {str(e)}"},
            status_code=500,
        )
    finally:
        if conn:
            conn.close()


async def create_message_api_route(request: Request) -> JSONResponse:
    """POST /api/messages — admin composes a message to a recipient.

    Body: {token, recipient_id, message_content, message_type?, priority?,
           subject?, parent_message_id?}
    Returns: {success, message_id, message}

    v5.0.22 (message threads + subjects):
      * `subject` — root-only one-liner; persisted verbatim when
        present. Force-NULLed for replies.
      * `parent_message_id` — when set, this message is a reply to
        the named root; subject is forced NULL regardless of input.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        recipient_id = data.get('recipient_id')
        content = data.get('message_content')
        message_type = data.get('message_type', 'text')
        priority = data.get('priority', 'normal')
        # v5.0.22 — message threads + subjects.
        explicit_subject = data.get('subject')
        parent_message_id = data.get('parent_message_id')

        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        if not recipient_id:
            return JSONResponse(
                {"error": "recipient_id is required"}, status_code=400
            )
        if not content:
            return JSONResponse(
                {"error": "message_content is required"}, status_code=400
            )
        if message_type not in _MESSAGE_TYPES:
            return JSONResponse(
                {"error": f"message_type must be one of {_MESSAGE_TYPES}"},
                status_code=400,
            )
        if priority not in _MESSAGE_PRIORITIES:
            return JSONResponse(
                {"error": f"priority must be one of {_MESSAGE_PRIORITIES}"},
                status_code=400,
            )

        import secrets as _secrets
        timestamp = datetime.datetime.now().isoformat()
        sender_id = auth_get_agent_id(admin_token) or "admin"

        conn = get_db_connection()
        cursor = conn.cursor()

        # Broadcast: recipient_id="*" fans out to every active worker
        # (admin excluded), mirroring the broadcast_message MCP tool.
        # One INSERT per recipient so the messages show up in the
        # listing keyed by their real recipient_id.
        if recipient_id == "*":
            from agent_mcp.core import globals as _g
            recipients: list[str] = []
            for _tok, agent_data in _g.active_agents.items():
                rid = agent_data.get("agent_id")
                if rid and rid != "admin" and rid != sender_id:
                    recipients.append(rid)

            # PR-G4 cutover: one bulk INSERT via the agent_messages_db
            # action module (executemany-style under the hood). The
            # surrounding action-log INSERT keeps its raw cursor so
            # everything still commits atomically with the broadcast.
            from ..db.actions.agent_messages_db import bulk_insert_messages

            sent_ids: list[str] = []
            broadcast_rows: list[dict] = []
            for rid in recipients:
                msg_id = f"msg_{_secrets.token_hex(8)}"
                sent_ids.append(msg_id)
                broadcast_rows.append({
                    "message_id": msg_id,
                    "sender_id": sender_id,
                    "recipient_id": rid,
                    "message_content": content,
                    "message_type": message_type,
                    "priority": priority,
                    "timestamp": timestamp,
                    "delivered": False,
                    "read": False,
                })
            bulk_insert_messages(broadcast_rows)
            log_agent_action_to_db(
                cursor, sender_id, "broadcast_message_via_dashboard",
                details={"recipients": recipients,
                         "sent_count": len(sent_ids)},
            )
            conn.commit()
            return JSONResponse({
                "success": True,
                "broadcast": True,
                "sent_count": len(sent_ids),
                "message_ids": sent_ids,
                "message": f"Broadcast sent to {len(sent_ids)} agents",
            })

        message_id = f"msg_{_secrets.token_hex(8)}"

        # v5.0.22 effective-subject computation. Three branches —
        # mirrors send_agent_message_tool_impl exactly:
        #   1. Reply (parent set) → subject NULL regardless of body.
        #   2. Explicit subject → verbatim.
        #   3. Root w/o subject → Ollama suggest_subject if
        #      AGENT_MCP_SUBJECT_MODEL is set; otherwise truncated body.
        effective_subject: str | None
        if parent_message_id:
            effective_subject = None
        elif explicit_subject:
            effective_subject = explicit_subject
        else:
            suggested: str | None = None
            if os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
                from ..features.message_suggestions import suggest_subject
                suggested = await suggest_subject(content)
            if suggested:
                effective_subject = suggested
            else:
                effective_subject = (
                    content[:50] + "..." if len(content) > 50 else content
                )

        cursor.execute(
            """
            INSERT INTO agent_messages (
                message_id, sender_id, recipient_id, message_content,
                message_type, priority, timestamp, delivered, read,
                subject, parent_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender_id, recipient_id, content,
             message_type, priority, timestamp, 0, 0,
             effective_subject, parent_message_id),
        )
        log_agent_action_to_db(
            cursor, sender_id, "sent_message_via_dashboard",
            details={"message_id": message_id, "recipient": recipient_id},
        )
        conn.commit()

        return JSONResponse({
            "success": True,
            "message_id": message_id,
            "message": f"Message sent to {recipient_id}",
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error sending message: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to send message: {str(e)}"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


async def suggest_subject_api_route(request: Request) -> JSONResponse:
    """POST /api/messages/suggest-subject — Ollama-backed subject helper.

    Body: {token, content}
    Returns: {"subject": "<string>"}   on success
             {"subject": null}          when AGENT_MCP_SUBJECT_MODEL is
                                        unset OR the helper failed.
    Status 401 when the token is missing/invalid (any agent token,
    not just admin — the helper is read-only and cheap).

    Why graceful degrade (200 + null) rather than 503: the dashboard
    treats this as a hint, not a hard requirement. If the helper is
    down, the user types a subject by hand; we don't want to colour
    that "subject is empty" path as an error in the network panel.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    token = data.get('token')
    # `verify_token(t)` with no required_role accepts any valid agent
    # token (admin or worker). Returns False on missing/invalid.
    if not token or not verify_token(token):
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    content = (data.get('content') or "").strip()
    if not content:
        return JSONResponse({"subject": None})

    # Short-circuit when no Ollama backend is configured. Saves the
    # helper import + the no-op call. Matches the gate inside
    # send_agent_message_tool_impl.
    if not os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
        return JSONResponse({"subject": None})

    try:
        from ..features.message_suggestions import suggest_subject
        subject = await suggest_subject(content)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("suggest_subject endpoint helper raised: %s", e)
        subject = None

    return JSONResponse({"subject": subject})


async def patch_message_api_route(request: Request) -> JSONResponse:
    """PATCH/DELETE /api/messages/{message_id}.

    PATCH flips read/delivered. DELETE removes the row (used by the
    dashboard's row-level + bulk delete actions). Admin-only.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method not in ('PATCH', 'DELETE'):
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "message_id is required in URL"}, status_code=400)
    message_id = path_parts[-1]

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')
        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id FROM agent_messages WHERE message_id = ?",
            (message_id,),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Message not found"}, status_code=404)

        if request.method == 'DELETE':
            cursor.execute(
                "DELETE FROM agent_messages WHERE message_id = ?",
                (message_id,),
            )
            log_agent_action_to_db(
                cursor, auth_get_agent_id(admin_token) or "admin",
                "deleted_message_via_dashboard",
                details={"message_id": message_id},
            )
            conn.commit()
            return JSONResponse({"success": True, "deleted": message_id})

        # PATCH
        updates: list[tuple[str, object]] = []
        if 'read' in data:
            updates.append(("read", 1 if data['read'] else 0))
        if 'delivered' in data:
            updates.append(("delivered", 1 if data['delivered'] else 0))
        if not updates:
            return JSONResponse(
                {"error": "no updatable field provided (read, delivered)"},
                status_code=400,
            )

        set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
        params = [v for _, v in updates] + [message_id]
        cursor.execute(
            f"UPDATE agent_messages SET {set_clause} WHERE message_id = ?",
            params,
        )
        log_agent_action_to_db(
            cursor, auth_get_agent_id(admin_token) or "admin",
            "updated_message", details={"message_id": message_id,
                                        "fields": [c for c, _ in updates]},
        )
        conn.commit()
        return JSONResponse({"success": True})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error patching message: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to patch message: {str(e)}"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


# Add the memory CRUD routes
routes.extend([
    Route('/api/memories', endpoint=create_memory_api_route, name="create_memory_api", methods=['POST', 'OPTIONS']),
    Route('/api/memories/{context_key}', endpoint=update_memory_api_route, name="update_memory_api", methods=['PUT', 'OPTIONS']),
    Route('/api/memories/{context_key}', endpoint=delete_memory_api_route, name="delete_memory_api", methods=['DELETE', 'OPTIONS']),
    Route('/api/context-data', endpoint=context_data_api_route, name="context_data_api", methods=['GET', 'OPTIONS']),
    # Task CRUD (issue C). GET /api/tasks (list) already exists earlier.
    Route('/api/tasks', endpoint=create_task_api_route, name="create_task_api", methods=['POST', 'OPTIONS']),
    Route('/api/tasks/{task_id}', endpoint=delete_task_api_route, name="delete_task_api", methods=['DELETE', 'OPTIONS']),
    # Messages CRUD (Phase 6 PR #20 / issue P).
    # Listing uses POST /api/messages/query (not GET) because browsers
    # strip GET bodies per the Fetch spec; declared before the
    # compose route so the more specific path matches first.
    Route('/api/messages/query', endpoint=list_messages_api_route, name="list_messages_api", methods=['POST', 'OPTIONS']),
    # Participants endpoint: live agents (status != terminated) + tombstones
    # for purged agents (PR C cascade). Sources the Sender/Recipient
    # filter dropdowns so terminated agents don't ghost the UI.
    Route('/api/messages/participants', endpoint=list_participants_api_route, name="list_participants_api", methods=['POST', 'OPTIONS']),
    Route('/api/messages', endpoint=create_message_api_route, name="create_message_api", methods=['POST', 'OPTIONS']),
    # v5.0.22: subject-suggest helper. Declared BEFORE
    # /api/messages/{message_id} so the static path matches before the
    # dynamic one (Starlette walks routes in registration order).
    Route('/api/messages/suggest-subject', endpoint=suggest_subject_api_route, name="suggest_subject_api", methods=['POST', 'OPTIONS']),
    Route('/api/messages/{message_id}', endpoint=patch_message_api_route, name="patch_message_api", methods=['PATCH', 'DELETE', 'OPTIONS']),
])

# Add the sample data route
routes.append(Route('/api/create-sample-memories', endpoint=create_sample_memories_route, name="create_sample_memories", methods=['POST', 'OPTIONS']))