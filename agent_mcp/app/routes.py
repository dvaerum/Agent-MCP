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

# Import tool implementations that these dashboard APIs will call
from ..tools.admin_tools import (
    create_agent_tool_impl,
    terminate_agent_tool_impl
)
import mcp.types as mcp_types # For handling the result from tool_impl

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
    # Without a `status` query param, returns every agent row plus the
    # synthetic "Admin" row used by the dashboard graph (back-compat).
    #
    # With `status=<value>`, returns only agent rows whose status
    # matches exactly. The synthetic Admin row (status='system') is
    # also filtered out — only rows whose status equals the filter
    # value survive. This shape replaces the router's `list_agents`
    # synthetic tool (Phase 7c, Q7.2 in plan).
    status_filter: Optional[str] = request.query_params.get('status')
    agents_list_data: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if status_filter is None:
            admin_style = get_node_style('admin')
            agents_list_data.append({
                'agent_id': 'Admin', 'status': 'system', 'color': admin_style.get('color', '#607D8B'),
                'created_at': 'N/A', 'current_task': 'N/A'
            })
            cursor.execute("SELECT agent_id, status, color, created_at, current_task FROM agents ORDER BY created_at DESC")
        else:
            cursor.execute(
                "SELECT agent_id, status, color, created_at, current_task "
                "FROM agents WHERE status = ? ORDER BY created_at DESC",
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

# Original: main.py lines 2061-2099 (terminate_agent_api function)
async def terminate_agent_dashboard_api_route(request: Request) -> JSONResponse:
    """Dashboard API endpoint to terminate an agent. Calls the admin tool internally."""
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await get_sanitized_json_body(request)
        admin_auth_token = data.get("token")
        agent_id_to_terminate = data.get("agent_id")

        if not verify_token(admin_auth_token, "admin"):
            return JSONResponse({"message": "Unauthorized: Invalid admin token for API call"}, status_code=401)

        if not agent_id_to_terminate:
            return JSONResponse({"message": "Agent ID to terminate is required"}, status_code=400)

        tool_args = {
            "token": admin_auth_token, # Tool impl will verify again
            "agent_id": agent_id_to_terminate
        }

        result_list: List[mcp_types.TextContent] = await terminate_agent_tool_impl(tool_args)

        if result_list and result_list[0].text.startswith(f"Agent '{agent_id_to_terminate}' terminated"):
            return JSONResponse({"message": f"Agent '{agent_id_to_terminate}' terminated successfully via dashboard API."})
        else:
            error_message = result_list[0].text if result_list else "Unknown error terminating agent."
            status_code = 400
            if "Unauthorized" in error_message: status_code = 401
            if "not found" in error_message: status_code = 404
            return JSONResponse({"message": error_message}, status_code=status_code)
            
    except ValueError as e_val: # From get_sanitized_json_body
        return JSONResponse({"message": str(e_val)}, status_code=400)
    except Exception as e:
        logger.error(f"Error in terminate_agent_dashboard_api_route: {e}", exc_info=True)
        return JSONResponse({"message": f"Error terminating agent via dashboard API: {str(e)}"}, status_code=500)


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
        editable = ('capabilities', 'color', 'working_directory')
        updates = {k: data[k] for k in editable if k in data}
        if not updates:
            return JSONResponse(
                {"error": "No editable fields supplied. Accepts any of: "
                          + ", ".join(editable)},
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
async def all_data_api_route(request: Request) -> JSONResponse:
    """Get all data in one call for caching on frontend"""
    if request.method == 'OPTIONS':
        return await handle_options(request)
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all agents with their tokens
        cursor.execute("SELECT * FROM agents ORDER BY created_at DESC")
        agents_data = []
        for row in cursor.fetchall():
            agent_dict = dict(row)
            agent_id = agent_dict['agent_id']
            
            # Find token for this agent from active_agents
            agent_token = None
            for token, data in g.active_agents.items():
                if data.get("agent_id") == agent_id and data.get("status") != "terminated":
                    agent_token = token
                    break
            
            agent_dict['auth_token'] = agent_token
            agents_data.append(agent_dict)
        
        # Add admin as special agent
        agents_data.insert(0, {
            'agent_id': 'Admin',
            'status': 'system',
            'auth_token': g.admin_token,
            'created_at': 'N/A',
            'current_task': 'N/A'
        })
        
        # Get all tasks
        cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        tasks_data = [dict(row) for row in cursor.fetchall()]
        
        # Get all context entries via the ORM (Phase 7a).
        with SessionLocal() as ctx_session:
            ctx_rows = (
                ctx_session.query(ProjectContext)
                .order_by(ProjectContext.last_updated.desc())
                .all()
            )
            context_data = [
                {
                    "context_key": r.context_key,
                    "value": r.value,
                    "last_updated": r.last_updated,
                    "updated_by": r.updated_by,
                    "description": r.description,
                }
                for r in ctx_rows
            ]

        # Get recent agent actions (last 100)
        cursor.execute("""
            SELECT * FROM agent_actions 
            ORDER BY timestamp DESC 
            LIMIT 100
        """)
        actions_data = [dict(row) for row in cursor.fetchall()]
        
        # Get file metadata
        cursor.execute("SELECT * FROM file_metadata")
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
                .order_by(ProjectContext.last_updated.desc())
                .all()
            )
            context_data = [
                {
                    "context_key": r.context_key,
                    "value": r.value,
                    "last_updated": r.last_updated,
                    "updated_by": r.updated_by,
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

# --- Route Definitions List ---
routes = [
    Route('/api/all-data', endpoint=all_data_api_route, name="all_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/status', endpoint=simple_status_api_route, name="simple_status_api", methods=['GET', 'OPTIONS']),
    Route('/api/graph-data', endpoint=graph_data_api_route, name="graph_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/task-tree-data', endpoint=task_tree_data_api_route, name="task_tree_data_api", methods=['GET', 'OPTIONS']),
    Route('/api/node-details', endpoint=node_details_api_route, name="node_details_api", methods=['GET', 'OPTIONS']),
    Route('/api/agents', endpoint=agents_list_api_route, name="agents_list_api", methods=['GET', 'OPTIONS']),
    Route('/api/agents-list', endpoint=agents_list_api_route, name="agents_list_api_legacy", methods=['GET', 'OPTIONS']),
    Route('/api/tokens', endpoint=tokens_api_route, name="tokens_api", methods=['GET', 'OPTIONS']),
    Route('/api/tasks', endpoint=all_tasks_api_route, name="all_tasks_api", methods=['GET', 'OPTIONS']),
    Route('/api/tasks-all', endpoint=all_tasks_api_route, name="all_tasks_api_legacy", methods=['GET', 'OPTIONS']),
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
                        last_updated=current_time,
                        updated_by=memory['updated_by'],
                        description=memory['description'],
                    )
                )
            else:
                existing.value = memory['value']
                existing.last_updated = current_time
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
                last_updated=current_time,
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
        row.last_updated = current_time
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
    """Delete a memory entry"""
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if request.method != 'DELETE':
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

        # Delete the memory
        session.delete(row)
        session.flush()

        # Log the action
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(cursor, requesting_admin_id, "deleted_memory", details={"context_key": context_key})
        session.commit()

        return JSONResponse({
            "success": True,
            "message": f"Memory '{context_key}' deleted successfully"
        })

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if session is not None:
            session.rollback()
        logger.error(f"Error deleting memory: {e}", exc_info=True)
        return JSONResponse({"error": f"Failed to delete memory: {str(e)}"}, status_code=500)
    finally:
        if session is not None:
            session.close()

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
           "assigned_to"?, "parent_task"?}
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
                parent_task, child_tasks, depends_on_tasks, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, title, description, assigned_to, requesting_admin_id,
                status, priority, now, now,
                parent_task, '[]', '[]', '[]',
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
    """Delete a task by ID. Admin token in JSON body."""
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'DELETE':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    # Extract task_id from URL path (last segment).
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "task_id is required in URL"}, status_code=400)
    task_id = path_parts[-1]

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        admin_token = data.get('token')

        if not verify_token(admin_token, required_role='admin'):
            return JSONResponse({"error": "Invalid admin token"}, status_code=403)

        requesting_admin_id = auth_get_agent_id(admin_token)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,))
        if not cursor.fetchone():
            return JSONResponse({"error": "Task not found"}, status_code=404)

        cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        log_agent_action_to_db(
            cursor, requesting_admin_id, "deleted_task", task_id=task_id,
        )
        conn.commit()

        return JSONResponse({
            "success": True,
            "message": f"Task '{task_id}' deleted successfully",
        })

    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error deleting task: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Failed to delete task: {str(e)}"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


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

        # Live agents: anything that hasn't been terminated. We keep
        # 'pending'/'failed'/etc. visible so historical messages from
        # those agents stay filterable — only 'terminated' is the ghost
        # state Dennis flagged.
        cursor.execute(
            "SELECT agent_id, status FROM agents "
            "WHERE status IS NULL OR status != 'terminated' "
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

    Body: {token, recipient_id, message_content, message_type?, priority?}
    Returns: {success, message_id, message}
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

            sent_ids: list[str] = []
            for rid in recipients:
                msg_id = f"msg_{_secrets.token_hex(8)}"
                cursor.execute(
                    """
                    INSERT INTO agent_messages (
                        message_id, sender_id, recipient_id, message_content,
                        message_type, priority, timestamp, delivered, read
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (msg_id, sender_id, rid, content,
                     message_type, priority, timestamp, 0, 0),
                )
                sent_ids.append(msg_id)
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
        cursor.execute(
            """
            INSERT INTO agent_messages (
                message_id, sender_id, recipient_id, message_content,
                message_type, priority, timestamp, delivered, read
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender_id, recipient_id, content,
             message_type, priority, timestamp, 0, 0),
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
    Route('/api/messages/{message_id}', endpoint=patch_message_api_route, name="patch_message_api", methods=['PATCH', 'DELETE', 'OPTIONS']),
])

# Add the sample data route
routes.append(Route('/api/create-sample-memories', endpoint=create_sample_memories_route, name="create_sample_memories", methods=['POST', 'OPTIONS']))