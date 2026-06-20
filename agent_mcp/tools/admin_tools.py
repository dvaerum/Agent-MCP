# Agent-MCP/mcp_template/mcp_server_src/tools/admin_tools.py
import json
import datetime
import subprocess  # For launching Cursor (will be commented out)
import os
import sqlite3
from typing import List, Dict, Any, Optional

import mcp.types as mcp_types  # Assuming this is your mcp.types path

from .registry import register_tool
from ..core.config import logger, AGENT_COLORS  # AGENT_COLORS for create_agent
from ..core import globals as g
from ..core.auth import generate_token  # For create_agent, terminate_agent
from ..core.authorize import requires_role  # @requires_role("operator") gates entry
from ..utils.audit_utils import log_audit
from ..utils.project_utils import generate_system_prompt  # For create_agent
from ..runtime.agent_runtime import (
    is_tmux_available,
    agent_setup_delay,
    create_tmux_session,
    kill_tmux_session,
    session_exists,
    sanitize_session_name,
    list_tmux_sessions,
    send_prompt_async,
    send_command_to_session,
)
from ..utils.prompt_templates import build_agent_prompt
from ..db.connection import get_db_connection, execute_db_write
from ..db.actions.agent_actions_db import log_agent_action_to_db  # For DB logging


def get_admin_token_suffix(admin_token: str) -> str:
    """
    Extract the last 4 characters from admin token for session naming.

    Args:
        admin_token: The admin authentication token

    Returns:
        Last 4 characters of the token in lowercase
    """
    if not admin_token or len(admin_token) < 4:
        return "0000"  # Fallback for invalid tokens
    return admin_token[-4:].lower()


def create_agent_session_name(agent_id: str, admin_token: str) -> str:
    """
    Create agent session name in format: agent_id-suffix
    where suffix is the last 4 characters of the admin token.

    Args:
        agent_id: The agent identifier
        admin_token: The admin authentication token

    Returns:
        Session name in format "agent_id-def2" where def2 is from admin token
    """
    suffix = get_admin_token_suffix(admin_token)
    clean_agent_id = sanitize_session_name(agent_id)
    return f"{clean_agent_id}-{suffix}"


# --- create_agent tool ---
# Original logic from main.py: lines 1060-1203 (create_agent_tool function)
@requires_role("operator")
async def create_agent_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    token = arguments.get("token")
    agent_id = arguments.get("agent_id")
    capabilities = arguments.get("capabilities")  # This was List[str]
    task_ids = arguments.get("task_ids")  # Required list of task IDs
    # Phase 2 Wave 2b: dashboard's Role dropdown reaches this tool
    # impl via ``create_agent_dashboard_api_route``. Validation
    # already ran at the API boundary; defence-in-depth: if a direct
    # MCP caller (or back-compat /api/create-agent path) supplies
    # something other than worker|manager, fall back to 'worker'
    # rather than letting the column CHECK constraint 500 us.
    agent_role = arguments.get("agent_role", "worker")
    if agent_role not in ("worker", "manager"):
        agent_role = "worker"

    # New prompt-related parameters
    prompt_template = arguments.get(
        "prompt_template", "worker_with_rag"
    )  # Default to RAG worker
    custom_prompt = arguments.get("custom_prompt")  # Custom prompt text
    send_prompt = arguments.get("send_prompt", True)  # Default to auto-send prompt
    prompt_delay = arguments.get("prompt_delay", 5)  # Default 5 second delay

    if not agent_id or not isinstance(agent_id, str):
        return [
            mcp_types.TextContent(
                type="text", text="Error: agent_id is required and must be a string."
            )
        ]

    # Forbid `[` and `]` in agent_id. The purge cascade rewrites
    # references to a deleted agent as the literal `[deleted-<id>]`;
    # allowing brackets in real agent_ids would create unparseable /
    # ambiguous tombstones.
    if "[" in agent_id or "]" in agent_id:
        return [
            mcp_types.TextContent(
                type="text",
                text=(
                    f"Error: invalid agent_id {agent_id!r}: `[` and `]` are "
                    "reserved characters (used by the purge-cascade "
                    "tombstone format `[deleted-<id>]`)."
                ),
            )
        ]

    # Validate task_ids parameter.
    #
    # task_ids is OPTIONAL — admin may create an unassigned agent and
    # wire up tasks later (this is how the dashboard's Deploy modal
    # creates workers; tasks are assigned via the Tasks page). When
    # provided, it must be a list of strings. The unbundled-create
    # contract was the original shape (pre-July-2025); the brief
    # "task_ids required" window broke the dashboard's Deploy button
    # entirely (no place in the form to enter task IDs). See
    # tests/test_dashboard_create_agent_endpoint.py.
    if task_ids is None:
        task_ids = []

    if not isinstance(task_ids, list):
        return [
            mcp_types.TextContent(
                type="text",
                text="Error: task_ids must be a list of task IDs (or omitted).",
            )
        ]

    # Validate each task_id is a string
    for task_id in task_ids:
        if not isinstance(task_id, str):
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Error: All task IDs must be strings. Found: {type(task_id).__name__}",
                )
            ]

    # Check in-memory map first (main.py:1072)
    if agent_id in g.agent_working_dirs:
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Agent '{agent_id}' already exists (in active memory).",
            )
        ]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Double check in DB (main.py:1077-1081)
        cursor.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,))
        if cursor.fetchone():
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Agent '{agent_id}' already exists (in database).",
                )
            ]

        # Validate task existence and availability
        for task_id in task_ids:
            cursor.execute(
                "SELECT task_id, assigned_to, status FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            task_row = cursor.fetchone()
            if not task_row:
                return [
                    mcp_types.TextContent(
                        type="text",
                        text=f"Error: Task '{task_id}' not found in database.",
                    )
                ]

            task_data = dict(task_row)

            # Check if task is already assigned
            if task_data.get("assigned_to") is not None:
                return [
                    mcp_types.TextContent(
                        type="text",
                        text=f"Error: Task '{task_id}' is already assigned to agent '{task_data['assigned_to']}'.",
                    )
                ]

            # Check if task is in a valid state for assignment
            task_status = task_data.get("status", "").lower()
            if task_status not in ["created", "unassigned"]:
                return [
                    mcp_types.TextContent(
                        type="text",
                        text=f"Error: Task '{task_id}' has status '{task_status}' and cannot be assigned. Only tasks with status 'created' or 'unassigned' can be assigned.",
                    )
                ]

        # Generate token and prepare data (main.py:1089-1092)
        new_agent_token = generate_token()
        created_at_iso = datetime.datetime.now().isoformat()
        # Event-coord PR-1: normalize capabilities once at write time
        # (strip + lowercase + dedupe, preserve insertion order). All
        # subsequent read paths can treat the stored JSON as canonical
        # — no read-time normalization fallbacks.
        from agent_mcp.utils.capability_normalization import normalize_capabilities

        normalized_caps = normalize_capabilities(capabilities)
        capabilities_json = json.dumps(normalized_caps)
        status = "created"  # Or "active" immediately? Original used "created".

        # Assign a color (main.py:1095-1097)
        agent_color = AGENT_COLORS[g.agent_color_index % len(AGENT_COLORS)]
        g.agent_color_index += 1

        # Determine working directory - all agents use shared project directory
        # MCP_PROJECT_DIR is set by cli.py or server startup.
        project_dir_env = os.environ.get("MCP_PROJECT_DIR")
        if not project_dir_env:
            logger.error(
                "MCP_PROJECT_DIR environment variable not set. Cannot determine agent working directory."
            )
            return [
                mcp_types.TextContent(
                    type="text",
                    text="Server configuration error: MCP_PROJECT_DIR not set.",
                )
            ]

        # All agents work in the same shared directory with file-level locking
        agent_working_dir_abs = os.path.abspath(project_dir_env)

        # Ensure the working directory exists
        try:
            os.makedirs(agent_working_dir_abs, exist_ok=True)
        except OSError as e:
            logger.error(
                f"Failed to create working directory {agent_working_dir_abs} for agent {agent_id}: {e}"
            )
            return [
                mcp_types.TextContent(
                    type="text", text=f"Error creating working directory: {e}"
                )
            ]

        # PR 6: agent INSERT goes through agent_repo with the caller's
        # cursor so the agent row lands in the same transaction as the
        # subsequent agent_actions audit log + task UPDATEs. The repo
        # defers cache write + EventBus publish until we call
        # `upsert_cache` post-commit (see below) — that way a rollback
        # can't desync the cache.
        from ..repositories import agent_repo, task_repo

        try:
            agent_repo.create(
                token=new_agent_token,
                agent_id=agent_id,
                capabilities=normalized_caps,
                status=status,
                current_task=None,
                working_directory=agent_working_dir_abs,
                color=agent_color,
                agent_role=agent_role,
                connection=cursor,
            )
        except ValueError as ve:
            # Server-side agent_id regex rejection (VM e2e fix
            # 2026-06-16). The repo raises BEFORE any DB write, so the
            # cursor's open transaction has nothing to roll back beyond
            # the BEGIN itself — but unwind the conn defensively.
            try:
                conn.rollback()
            except Exception:
                pass
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Error: {ve}",
                )
            ]

        # Log action to agent_actions table (main.py:1119)
        log_agent_action_to_db(
            cursor,
            "admin",
            "created_agent",
            details={
                "agent_id": agent_id,
                "color": agent_color,
                "wd": agent_working_dir_abs,
            },
        )

        # Assign tasks to the agent atomically — each UPDATE goes
        # through task_repo with the same cursor so the wider
        # transaction stays intact. The repo defers cache + publish on
        # the connection= path; we reconcile the in-memory cache after
        # commit below.
        assigned_tasks = []
        for task_id in task_ids:
            updated = task_repo.update_fields(
                task_id,
                {"assigned_to": agent_id, "status": "pending"},
                connection=cursor,
            )
            if not updated:
                # Should not happen since we validated earlier.
                raise Exception(
                    f"Failed to assign task '{task_id}' to agent '{agent_id}'"
                )
            assigned_tasks.append(task_id)

            # Log task assignment action
            log_agent_action_to_db(
                cursor,
                "admin",
                "assigned_task",
                details={
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "assignment_mode": "agent_creation",
                },
            )

        # Update agent with current task (set to first task if multiple).
        # PR 6: now goes through agent_repo with the caller's cursor —
        # the seam handles the raw-SQL UPDATE atomically with the
        # surrounding transaction.
        if assigned_tasks:
            agent_repo.update_field(
                agent_id, "current_task", assigned_tasks[0],
                connection=cursor,
            )

        # Commit the transaction (agent creation + task assignments)
        conn.commit()

        # PR 6: now that the transaction committed, reconcile caches
        # through the repos. upsert_cache is the documented escape
        # hatch for callers that own their own transactions.
        agent_repo.upsert_cache({
            "token": new_agent_token,
            "agent_id": agent_id,
            "capabilities": normalized_caps,
            "created_at": created_at_iso,
            "status": status,
            "current_task": assigned_tasks[0] if assigned_tasks else None,
            "color": agent_color,
            "working_directory": agent_working_dir_abs,
            "terminated_at": None,
            "updated_at": created_at_iso,
        })
        # Refresh task cache from DB so the assigned_to/status/updated_at
        # all reflect what landed. Cheap (one PK lookup per task).
        for task_id in assigned_tasks:
            fresh = task_repo.get_by_id(task_id)
            if fresh is not None:
                task_repo.upsert_cache(fresh)

        # Log to audit log file (main.py:1136-1144)
        log_audit(
            "admin",
            "create_agent",
            {
                "agent_id": agent_id,
                "capabilities": normalized_caps,
                "working_directory": agent_working_dir_abs,
                "assigned_color": agent_color,
                "assigned_tasks": assigned_tasks,
                "current_task": assigned_tasks[0] if assigned_tasks else None,
            },
        )

        # Generate system prompt (main.py:1147).
        # Wave 3 (prancy-napping-pie): ``generate_system_prompt`` no
        # longer takes a ``admin_token_runtime`` — the Admin / Worker
        # label is derived from ``agents.agent_role`` instead.
        system_prompt_str = generate_system_prompt(
            agent_id, new_agent_token
        )

        # Launch tmux session with Claude
        launch_status = "tmux session launching disabled - tmux not available."
        tmux_session_name = None

        if is_tmux_available():
            try:
                # Create sanitized session name.
                # Wave 3 (prancy-napping-pie): the session-name suffix
                # is the new agent's own token's last 4 chars (not
                # the calling admin token's, which used to flow in via
                # ``arguments["token"]`` and is no longer passed via
                # the REST seam). Per-agent suffix is also more useful
                # in tmux ls output — different agents now visibly
                # differ instead of all sharing the admin suffix.
                tmux_session_name = create_agent_session_name(
                    agent_id, new_agent_token
                )

                # Set up environment variables for the agent
                env_vars = {
                    "MCP_AGENT_ID": agent_id,
                    "MCP_AGENT_TOKEN": new_agent_token,
                    "MCP_SERVER_URL": f"http://localhost:{os.environ.get('PORT', '8080')}",
                    "MCP_WORKING_DIR": agent_working_dir_abs,
                }

                # Add the system token if this is an admin agent.
                # The env var is ``MCP_SYSTEM_TOKEN`` (renamed from
                # ``MCP_ADMIN_TOKEN`` in Phase 2 Wave 1b).
                # Wave 3 (prancy-napping-pie) dropped the legacy
                # ``MCP_ADMIN_TOKEN`` export — nothing in this repo
                # reads it back, and downstream agent startup scripts
                # have had a release on the new name. Wave 4 will
                # decide what happens to ``MCP_SYSTEM_TOKEN`` once
                # the admin pseudo-agent is gone.
                if (
                    agent_id.lower().startswith("admin")
                    and new_agent_token == g.system_token
                ):
                    env_vars["MCP_SYSTEM_TOKEN"] = g.system_token

                # Create the tmux session (without immediate command)
                if create_tmux_session(
                    session_name=tmux_session_name,
                    working_dir=agent_working_dir_abs,
                    command=None,  # Don't start Claude immediately
                    env_vars=env_vars,
                ):
                    # Track the tmux session in globals
                    g.agent_tmux_sessions[agent_id] = tmux_session_name

                    # Initial setup commands for visibility in tmux session
                    welcome_message = (
                        f"echo '=== Agent {agent_id} initialization starting ==='"
                    )
                    if send_command_to_session(tmux_session_name, welcome_message):
                        logger.info(f"✅ Sent welcome message to agent '{agent_id}'")
                    else:
                        logger.error(
                            f"❌ Failed to send welcome message to agent '{agent_id}'"
                        )

                    # Add setup delay to ensure commands execute properly
                    import time

                    def wait_for_command_completion(
                        session_name: str, delay: float = 1.0
                    ):
                        """Smart delay system - wait for command completion or timeout"""
                        time.sleep(delay)
                        # Could add tmux pane monitoring here in future for true completion detection

                    # Delay between tmux setup commands (see
                    # agent_setup_delay docstring); tests zero it.
                    setup_delay = agent_setup_delay()
                    wait_for_command_completion(tmux_session_name, setup_delay)

                    # Verify we're in the correct working directory
                    verify_command = f"echo 'Working directory:' && pwd"
                    if send_command_to_session(tmux_session_name, verify_command):
                        logger.info(
                            f"✅ Sent directory verification to agent '{agent_id}'"
                        )
                    else:
                        logger.error(
                            f"❌ Failed to send directory verification to agent '{agent_id}'"
                        )

                    wait_for_command_completion(tmux_session_name, setup_delay)

                    # Get server port for MCP registration
                    server_port = os.environ.get("PORT", "8080")
                    mcp_server_url = f"http://localhost:{server_port}/mcp"

                    # Log MCP server info
                    mcp_info_command = f"echo 'MCP Server URL: {mcp_server_url}'"
                    send_command_to_session(tmux_session_name, mcp_info_command)

                    wait_for_command_completion(tmux_session_name, setup_delay)

                    # Register MCP server connection
                    mcp_add_command = f"claude mcp add -t sse AgentMCP {mcp_server_url}"
                    logger.info(
                        f"Registering MCP server for agent '{agent_id}': {mcp_add_command}"
                    )

                    if not send_command_to_session(tmux_session_name, mcp_add_command):
                        logger.error(
                            f"Failed to register MCP server for agent '{agent_id}'"
                        )
                        base_status = (
                            f"❌ Failed to register MCP server for agent '{agent_id}'."
                        )
                    else:
                        # Add delay to ensure MCP registration completes
                        wait_for_command_completion(tmux_session_name, setup_delay)

                        # Verify MCP registration
                        verify_mcp_command = "claude mcp list"
                        logger.info(
                            f"Verifying MCP registration for agent '{agent_id}'"
                        )
                        send_command_to_session(tmux_session_name, verify_mcp_command)
                        wait_for_command_completion(tmux_session_name, setup_delay)

                        # Start Claude
                        start_claude_message = "echo '--- Starting Claude with MCP ---'"
                        send_command_to_session(tmux_session_name, start_claude_message)
                        wait_for_command_completion(tmux_session_name, setup_delay)

                        claude_command = "claude --dangerously-skip-permissions"
                        logger.info(
                            f"Starting Claude for agent '{agent_id}': {claude_command}"
                        )

                        if not send_command_to_session(
                            tmux_session_name, claude_command
                        ):
                            logger.error(
                                f"Failed to start Claude for agent '{agent_id}'"
                            )
                            base_status = f"❌ Failed to start Claude for agent '{agent_id}' after MCP registration."
                        else:
                            base_status = f"✅ tmux session '{tmux_session_name}' created for agent '{agent_id}' with MCP registration and Claude."

                            # Log completion message to tmux session (will appear before Claude starts)
                            completion_message = f"echo '=== Agent {agent_id} setup complete - Claude starting ==='"
                            send_command_to_session(
                                tmux_session_name, completion_message
                            )

                    # Send prompt if requested
                    prompt_status = ""
                    if send_prompt:
                        try:
                            # Build the prompt using the template system.
                            # Wave 3 (prancy-napping-pie): dropped the
                            # ``admin_token`` arg; no template still
                            # substitutes ``{admin_token}``.
                            agent_prompt = build_agent_prompt(
                                agent_id=agent_id,
                                agent_token=new_agent_token,
                                template_name=prompt_template,
                                custom_prompt=custom_prompt,
                            )

                            if agent_prompt:
                                # Send prompt asynchronously
                                send_prompt_async(
                                    tmux_session_name, agent_prompt, prompt_delay
                                )
                                prompt_status = f" Prompt will be sent in {prompt_delay} seconds using '{prompt_template}' template."
                                logger.info(
                                    f"Scheduled prompt delivery for agent '{agent_id}' using template '{prompt_template}'"
                                )
                            else:
                                prompt_status = f" ❌ Failed to build prompt using template '{prompt_template}'."
                                logger.error(
                                    f"Failed to build prompt for agent '{agent_id}' using template '{prompt_template}'"
                                )
                        except Exception as e_prompt:
                            prompt_status = (
                                f" ❌ Error setting up prompt: {str(e_prompt)}"
                            )
                            logger.error(
                                f"Error setting up prompt for agent '{agent_id}': {e_prompt}"
                            )

                    launch_status = base_status + prompt_status
                    logger.info(
                        f"tmux session '{tmux_session_name}' launched for agent '{agent_id}'"
                    )
                else:
                    launch_status = (
                        f"❌ Failed to create tmux session for agent '{agent_id}'."
                    )
                    logger.error(launch_status)

            except Exception as e_launch:
                launch_status = f"❌ Failed to launch tmux session: {str(e_launch)}"
                logger.error(launch_status, exc_info=True)
        else:
            logger.warning(
                "tmux is not available - agent session cannot be launched automatically"
            )
            launch_status = "⚠️ tmux not available - manual agent setup required."

        # All agents work in shared project directory with file-level locking

        # Log to console (main.py:1169-1174)
        console_output = (
            f"\n=== Agent '{agent_id}' Created ===\n"
            f"Token: {new_agent_token}\n"
            f"Assigned Color: {agent_color}\n"
            f"Working Directory: {agent_working_dir_abs}\n"
            f"Assigned Tasks: {', '.join(assigned_tasks)}\n"
            f"Current Task: {assigned_tasks[0] if assigned_tasks else 'None'}\n"
            f"{launch_status}\n"
            f"=========================\n"
            f"=== System Prompt for {agent_id} ===\n{system_prompt_str}\n"
            f"========================="
        )
        logger.info(
            f"Agent '{agent_id}' created. Token: {new_agent_token}, Color: {agent_color}, WD: {agent_working_dir_abs}"
        )
        print(console_output)  # For direct CLI feedback

        return [
            mcp_types.TextContent(
                type="text",
                text=f"Agent '{agent_id}' created successfully.\n"
                f"Token: {new_agent_token}\n"
                f"Assigned Color: {agent_color}\n"
                f"Working Directory: {agent_working_dir_abs}\n"
                f"Assigned Tasks: {', '.join(assigned_tasks)}\n"
                f"Current Task: {assigned_tasks[0] if assigned_tasks else 'None'}\n"
                f"{launch_status}\n\n"
                f"System Prompt:\n{system_prompt_str}",
            )
        ]

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            f"Database error creating agent {agent_id}: {e_sql}", exc_info=True
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error creating agent: {e_sql}"
            )
        ]
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Unexpected error creating agent {agent_id}: {e}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error creating agent: {e}"
            )
        ]
    finally:
        if conn:
            conn.close()


# --- view_status tool ---
# Original logic from main.py: lines 1242-1268 (view_status_tool function)
@requires_role("operator")
async def view_status_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    log_audit("admin", "view_status", {})  # main.py:1249

    # Build agent status from g.active_agents and g.agent_working_dirs (main.py:1251-1259)
    agent_status_dict = {}
    for agent_tkn, agent_data in g.active_agents.items():
        agent_id = agent_data.get("agent_id")
        if agent_id:  # Should always be present if agent_data is valid
            agent_status_dict[agent_id] = {
                "status": agent_data.get("status", "unknown"),
                "current_task": agent_data.get("current_task"),
                "capabilities": agent_data.get("capabilities", []),
                "working_directory": g.agent_working_dirs.get(agent_id, "N/A"),
                "color": agent_data.get(
                    "color", "N/A"
                ),  # Added color from active_agents
            }

    # Server uptime was N/A in original (main.py:1264)
    # We need a server start time global to calculate this, or pass it from app lifecycle.
    # For now, keeping it N/A for 1-to-1.
    server_start_time_iso = (
        g.server_start_time if hasattr(g, "server_start_time") else None
    )
    uptime_str = "N/A"
    if server_start_time_iso:
        uptime_delta = datetime.datetime.now() - datetime.datetime.fromisoformat(
            server_start_time_iso
        )
        uptime_str = str(uptime_delta)

    # Get tmux session information
    tmux_info = {
        "tmux_available": is_tmux_available(),
        "tracked_sessions": len(g.agent_tmux_sessions),
        "active_sessions": [],
        "session_details": {},
    }

    if is_tmux_available():
        tmux_sessions = list_tmux_sessions()
        tmux_info["active_sessions"] = [s["name"] for s in tmux_sessions]
        tmux_info["session_details"] = {s["name"]: s for s in tmux_sessions}

        # Add tmux session info to agent details
        for agent_id, agent_data in agent_status_dict.items():
            if agent_id in g.agent_tmux_sessions:
                session_name = g.agent_tmux_sessions[agent_id]
                agent_data["tmux_session"] = session_name
                agent_data["session_active"] = (
                    session_name in tmux_info["active_sessions"]
                )
            else:
                agent_data["tmux_session"] = None
                agent_data["session_active"] = False

    status_payload = {  # main.py:1260-1266
        "active_connections": len(
            g.connections
        ),  # g.connections might be managed by SSE transport layer
        "active_agents_count": len(g.active_agents),
        "agents_details": agent_status_dict,
        "server_uptime": uptime_str,
        "file_map_size": len(g.file_map),
        "file_map_preview": {
            k: v for i, (k, v) in enumerate(g.file_map.items()) if i < 5
        },  # Preview first 5
        "tmux_info": tmux_info,
        # Consider adding task counts, DB status, RAG index status etc.
    }

    try:
        status_json = json.dumps(status_payload, indent=2)
    except TypeError as e:
        logger.error(f"Error serializing server status to JSON: {e}")
        status_json = f"Error creating status JSON: {e}"

    return [
        mcp_types.TextContent(type="text", text=f"MCP Server Status:\n{status_json}")
    ]


# --- terminate_agent tool ---
# Original logic from main.py: lines 1270-1316 (terminate_agent_tool function)
@requires_role("operator")
async def terminate_agent_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    agent_id_to_terminate = arguments.get("agent_id")

    if not agent_id_to_terminate or not isinstance(agent_id_to_terminate, str):
        return [
            mcp_types.TextContent(
                type="text", text="Error: agent_id to terminate is required."
            )
        ]

    # Find agent token from in-memory map (main.py:1279-1283)
    found_agent_token: Optional[str] = None
    for tkn, data in g.active_agents.items():
        if data.get("agent_id") == agent_id_to_terminate:
            found_agent_token = tkn
            break

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not found_agent_token:
            # Check DB if not found in memory (main.py:1285-1290)
            cursor.execute(
                "SELECT token FROM agents WHERE agent_id = ? AND status != ?",
                (agent_id_to_terminate, "terminated"),
            )
            row = cursor.fetchone()
            if row:
                # Agent exists in DB but not active memory. Proceed to terminate in DB.
                logger.warning(
                    f"Agent {agent_id_to_terminate} found in DB (token: {row['token']}) but not in active memory. Proceeding with DB termination."
                )
                # We don't have its token to remove from g.active_agents if it's not there.
            else:
                return [
                    mcp_types.TextContent(
                        type="text",
                        text=f"Agent '{agent_id_to_terminate}' not found or already terminated.",
                    )
                ]

        # PR 6: terminate UPDATE goes through agent_repo with the
        # caller's cursor so it stays atomic with the agent_actions
        # audit-log INSERT below. The repo defers cache eviction +
        # `agent.terminated` publish to the post-commit step.
        from ..repositories import agent_repo
        ok = agent_repo.terminate(
            agent_id_to_terminate, connection=cursor,
        )

        if (
            not ok and not found_agent_token
        ):  # If DB check didn't find it initially and update affected 0 rows
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Agent '{agent_id_to_terminate}' not found in DB or already terminated.",
                )
            ]

        log_agent_action_to_db(
            cursor,
            "admin",
            "terminated_agent",
            details={"agent_id": agent_id_to_terminate},
        )
        conn.commit()

        # Post-commit cache reconciliation through the repo. Mirrors
        # the manual evictions the legacy code did inline; the repo's
        # `evict_from_cache` handles both the token-keyed and
        # agent_id-keyed maps in lockstep.
        agent_repo.evict_from_cache(
            agent_id_to_terminate, token=found_agent_token,
        )

        # Release any files held by this agent from g.file_map
        files_released_count = 0
        for filepath, info in list(g.file_map.items()):  # Iterate over a copy
            if info.get("agent_id") == agent_id_to_terminate:
                del g.file_map[filepath]
                files_released_count += 1
        if files_released_count > 0:
            logger.info(
                f"Released {files_released_count} files held by terminated agent {agent_id_to_terminate}."
            )

        # Kill tmux session if it exists
        tmux_kill_status = ""
        if agent_id_to_terminate in g.agent_tmux_sessions:
            session_name = g.agent_tmux_sessions[agent_id_to_terminate]
            if kill_tmux_session(session_name):
                tmux_kill_status = f" Killed tmux session '{session_name}'."
                logger.info(
                    f"Killed tmux session '{session_name}' for agent '{agent_id_to_terminate}'"
                )
            else:
                tmux_kill_status = f" Failed to kill tmux session '{session_name}'."
                logger.warning(
                    f"Failed to kill tmux session '{session_name}' for agent '{agent_id_to_terminate}'"
                )

            # Remove from tracking regardless of kill success
            del g.agent_tmux_sessions[agent_id_to_terminate]
        else:
            # Try to kill session by agent_id in case tracking is out of sync
            sanitized_name = sanitize_session_name(agent_id_to_terminate)
            if session_exists(sanitized_name):
                if kill_tmux_session(sanitized_name):
                    tmux_kill_status = (
                        f" Killed orphaned tmux session '{sanitized_name}'."
                    )
                    logger.info(
                        f"Killed orphaned tmux session '{sanitized_name}' for agent '{agent_id_to_terminate}'"
                    )

        log_audit(
            "admin", "terminate_agent", {"agent_id": agent_id_to_terminate}
        )  # main.py:1313
        logger.info(f"Agent '{agent_id_to_terminate}' terminated successfully.")
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Agent '{agent_id_to_terminate}' terminated.{tmux_kill_status}",
            )
        ]

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            f"Database error terminating agent {agent_id_to_terminate}: {e_sql}",
            exc_info=True,
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error terminating agent: {e_sql}"
            )
        ]
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            f"Unexpected error terminating agent {agent_id_to_terminate}: {e}",
            exc_info=True,
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error terminating agent: {e}"
            )
        ]
    finally:
        if conn:
            conn.close()


# --- view_audit_log tool ---
# Original logic from main.py: lines 1387-1408 (view_audit_log_tool function)
@requires_role("operator")
async def view_audit_log_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    filter_agent_id = arguments.get("agent_id")  # Optional filter
    filter_action = arguments.get("action")  # Optional filter
    limit = arguments.get("limit", 50)  # Default limit 50

    # Validate limit
    try:
        limit = int(limit)
        if not (1 <= limit <= 200):  # Max 200 for safety
            limit = 50
    except ValueError:
        limit = 50

    # Filter the in-memory audit log (g.audit_log) (main.py:1394-1400)
    # For a more complete audit log, one might query the agent_actions table from DB.
    # The original tool only viewed the in-memory `audit_log`.

    # The original `audit_log` was a global list.
    # The `log_audit` function in `utils/audit_utils.py` appends to `g.audit_log`.
    # So, we read from `g.audit_log`.

    # Create a working copy for filtering
    current_audit_log_snapshot = list(g.audit_log)  # Filter from a snapshot

    filtered_log_entries = current_audit_log_snapshot
    if filter_agent_id:
        filtered_log_entries = [
            entry
            for entry in filtered_log_entries
            if entry.get("agent_id") == filter_agent_id
        ]
    if filter_action:
        filtered_log_entries = [
            entry
            for entry in filtered_log_entries
            if entry.get("action") == filter_action
        ]

    # Get the most recent entries up to the limit (main.py:1403)
    # Slicing from the end gives recent entries.
    limited_log_entries = filtered_log_entries[-limit:]

    # Log this action itself (main.py:1405)
    log_audit(
        "admin",
        "view_audit_log",
        {
            "filter_agent_id": filter_agent_id,
            "filter_action": filter_action,
            "limit": limit,
        },
    )

    try:
        log_json = json.dumps(limited_log_entries, indent=2)
    except TypeError as e:
        logger.error(f"Error serializing audit log to JSON: {e}")
        log_json = f"Error creating audit log JSON: {e}"

    return [
        mcp_types.TextContent(
            type="text",
            text=f"Audit Log ({len(limited_log_entries)} entries displayed, filtered by agent: {filter_agent_id or 'Any'}, action: {filter_action or 'Any'}):\n{log_json}",
        )
    ]


# --- get_agent_tokens tool ---
@requires_role("operator")
async def get_agent_tokens_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """
    Retrieve agent tokens with advanced filtering capabilities.
    Supports filtering by status, agent_id pattern, creation date range, and more.
    """
    # Extract and validate filter parameters
    filter_status = arguments.get(
        "filter_status"
    )  # e.g., "active", "terminated", "created"
    filter_agent_id_pattern = arguments.get(
        "filter_agent_id_pattern"
    )  # SQL LIKE pattern
    filter_created_after = arguments.get("filter_created_after")  # ISO format date
    filter_created_before = arguments.get("filter_created_before")  # ISO format date
    include_terminated = arguments.get("include_terminated", False)  # Boolean
    include_sensitive_data = arguments.get("include_sensitive_data", True)  # Boolean
    limit = arguments.get("limit", 50)  # Default limit
    offset = arguments.get("offset", 0)  # Pagination offset
    sort_by = arguments.get("sort_by", "created_at")  # Sort field
    sort_order = arguments.get("sort_order", "DESC")  # ASC or DESC

    # Validate parameters
    try:
        limit = int(limit)
        if not (1 <= limit <= 500):  # Max 500 for safety
            limit = 50
    except (ValueError, TypeError):
        limit = 50

    try:
        offset = int(offset)
        if offset < 0:
            offset = 0
    except (ValueError, TypeError):
        offset = 0

    # Validate sort parameters
    allowed_sort_fields = ["created_at", "updated_at", "agent_id", "status"]
    if sort_by not in allowed_sort_fields:
        sort_by = "created_at"

    if sort_order.upper() not in ["ASC", "DESC"]:
        sort_order = "DESC"

    try:
        # PR 6: filter + count go through AgentRepository.query — the
        # repo owns the WHERE-building loop and the pagination total.
        from ..repositories import agent_repo

        rows, total_count = agent_repo.query({
            "status": filter_status,
            "agent_id_pattern": filter_agent_id_pattern,
            "include_terminated": include_terminated,
            "created_after": filter_created_after,
            "created_before": filter_created_before,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
            "offset": offset,
        })

        # Mask sensitive data the same way the legacy inline path did.
        agents_data = []
        for row in rows:
            agent_data = dict(row)
            if not include_sensitive_data:
                if "token" in agent_data:
                    token_value = agent_data["token"]
                    if token_value and len(token_value) > 8:
                        agent_data["token"] = (
                            token_value[:4] + "..." + token_value[-4:]
                        )
                    else:
                        agent_data["token"] = "***"
            agents_data.append(agent_data)

        # Log this access
        log_audit(
            "admin",
            "get_agent_tokens",
            {
                "filter_status": filter_status,
                "filter_agent_id_pattern": filter_agent_id_pattern,
                "agents_returned": len(agents_data),
                "total_matching": total_count,
                "include_sensitive_data": include_sensitive_data,
            },
        )

        # Build response
        response_data = {
            "agents": agents_data,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total_count": total_count,
                "returned_count": len(agents_data),
                "has_more": offset + len(agents_data) < total_count,
            },
            "filters_applied": {
                "filter_status": filter_status,
                "filter_agent_id_pattern": filter_agent_id_pattern,
                "filter_created_after": filter_created_after,
                "filter_created_before": filter_created_before,
                "include_terminated": include_terminated,
                "include_sensitive_data": include_sensitive_data,
            },
            "sort": {"sort_by": sort_by, "sort_order": sort_order},
        }

        try:
            response_json = json.dumps(response_data, indent=2)
        except TypeError as e:
            logger.error(f"Error serializing agent tokens response to JSON: {e}")
            response_json = f"Error creating response JSON: {e}"

        return [
            mcp_types.TextContent(
                type="text",
                text=f"Agent Tokens ({len(agents_data)} of {total_count} total):\n{response_json}",
            )
        ]

    except sqlite3.Error as e_sql:
        logger.error(f"Database error retrieving agent tokens: {e_sql}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error retrieving agent tokens: {e_sql}"
            )
        ]
    except Exception as e:
        logger.error(f"Unexpected error retrieving agent tokens: {e}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error retrieving agent tokens: {e}"
            )
        ]


# --- relaunch_agent tool ---
@requires_role("operator")
async def relaunch_agent_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """
    Relaunch an existing agent by reusing its tmux session.
    Only works for agents with status: terminated, completed, failed, cancelled.
    Sends /clear to reset the session and sends a new prompt.
    """
    agent_id = arguments.get("agent_id")
    generate_new_token = arguments.get("generate_new_token", False)
    custom_prompt = arguments.get("custom_prompt")
    prompt_template = arguments.get("prompt_template", "worker_with_rag")

    if not agent_id:
        return [mcp_types.TextContent(type="text", text="Error: agent_id is required")]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if agent exists and get current status
        cursor.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
        agent_row = cursor.fetchone()
        if not agent_row:
            return [
                mcp_types.TextContent(type="text", text=f"Agent '{agent_id}' not found")
            ]

        agent_data = dict(agent_row)
        current_status = agent_data.get("status")

        # Only allow relaunch for certain statuses
        allowed_statuses = ["terminated", "completed", "failed", "cancelled"]
        if current_status not in allowed_statuses:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Cannot relaunch agent with status '{current_status}'. Allowed statuses: {', '.join(allowed_statuses)}",
                )
            ]

        # Check if tmux session still exists
        if agent_id not in g.agent_tmux_sessions:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Agent '{agent_id}' has no active tmux session to relaunch. Use create_agent instead.",
                )
            ]

        session_name = g.agent_tmux_sessions[agent_id]
        if not session_exists(session_name):
            # Clean up the dead session reference
            del g.agent_tmux_sessions[agent_id]
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Tmux session '{session_name}' for agent '{agent_id}' no longer exists. Use create_agent instead.",
                )
            ]

        # Send /clear command to reset the session
        clear_success = send_command_to_session(session_name, "/clear")
        if not clear_success:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Failed to send /clear command to session '{session_name}'",
                )
            ]

        # Generate new token if requested
        agent_token = agent_data.get("token")
        if generate_new_token:
            agent_token = generate_token()
            # `token` is deliberately OFF agent_repo.update_field's
            # allowlist by design (the token is the auth secret;
            # rotating it must not be reachable via the generic
            # field-update API). PR 8 (Agent flip) gives token rotation
            # its own dedicated surface — agent_repo.rotate_token —
            # so the write goes through the repo (with the caller's
            # cursor) instead of owning a raw UPDATE here. The cache
            # re-key is deferred to the caller's post-commit step;
            # the relaunch flow rebuilds the active-agents entry
            # below once the wider transaction commits.
            from ..repositories import agent_repo as _agent_repo
            _agent_repo.rotate_token(
                agent_id, agent_token, connection=cursor,
            )

        # PR 6: status flip goes through agent_repo with the caller's
        # cursor so it stays atomic with the audit-log INSERT below.
        from ..repositories import agent_repo
        updated_at_iso = datetime.datetime.now().isoformat()
        agent_repo.update_field(
            agent_id, "status", "active", connection=cursor,
        )

        # Build and send new prompt.
        # Wave 3 (prancy-napping-pie): pass the relaunched agent's
        # own token + agent_id; the previous code passed
        # ``(prompt_template, admin_token)`` positionally, which was
        # a long-standing bug — the function takes keyword args
        # ``agent_id, agent_token, template_name`` and the positional
        # pair landed on the wrong parameters. Fixed in-place.
        try:
            if custom_prompt:
                prompt_to_send = custom_prompt
            else:
                prompt_to_send = build_agent_prompt(
                    agent_id=agent_id,
                    agent_token=agent_token,
                    template_name=prompt_template,
                    custom_prompt=custom_prompt,
                )

            # Send the new prompt to restart the agent
            send_prompt_async(session_name, prompt_to_send, delay_seconds=2)

        except Exception as e_prompt:
            logger.error(f"Failed to build or send prompt for relaunch: {e_prompt}")
            # Revert status change via repo on the same cursor.
            agent_repo.update_field(
                agent_id, "status", current_status, connection=cursor,
            )
            conn.commit()
            return [
                mcp_types.TextContent(
                    type="text", text=f"Failed to send restart prompt: {e_prompt}"
                )
            ]

        # Update in-memory state
        if agent_token in g.active_agents:
            g.active_agents[agent_token]["status"] = "active"
            g.active_agents[agent_token]["updated_at"] = updated_at_iso
        else:
            # Add to active agents if not already there
            g.active_agents[agent_token] = {
                "agent_id": agent_id,
                "status": "active",
                "token": agent_token,
                "working_directory": agent_data.get("working_directory"),
                "capabilities": json.loads(agent_data.get("capabilities", "[]")),
                "updated_at": updated_at_iso,
            }

        # Log the action
        log_agent_action_to_db(
            cursor,
            "admin",
            "relaunch_agent",
            details={
                "agent_id": agent_id,
                "session_name": session_name,
                "previous_status": current_status,
                "new_token_generated": generate_new_token,
                "prompt_template": prompt_template,
            },
        )

        conn.commit()

        log_audit(
            "admin",
            "relaunch_agent",
            {
                "agent_id": agent_id,
                "previous_status": current_status,
                "session_name": session_name,
                "new_token": generate_new_token,
            },
        )

        response_parts = [
            f"Agent '{agent_id}' successfully relaunched in session '{session_name}'",
            f"Previous status: {current_status} → active",
            f"Session cleared and new prompt sent",
        ]

        if generate_new_token:
            response_parts.append(f"New token generated: {agent_token}")
        else:
            response_parts.append(f"Using existing token: {agent_token}")

        return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            f"Database error relaunching agent {agent_id}: {e_sql}", exc_info=True
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error relaunching agent: {e_sql}"
            )
        ]
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            f"Unexpected error relaunching agent {agent_id}: {e}", exc_info=True
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error relaunching agent: {e}"
            )
        ]
    finally:
        if conn:
            conn.close()


# --- Register all admin tools ---
def register_admin_tools():
    register_tool(
        name="create_agent",
        description="Create a new agent with the specified ID, capabilities, and prompt configuration. task_ids is optional — pass a non-empty list to bundle initial task assignment, or omit to create an idle/unassigned agent (tasks can be assigned later via the Tasks page). Agents work in the shared project directory with file-level locking for coordination.",
        input_schema={  # Enhanced with prompt template support; task_ids OPTIONAL.
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the agent",
                },
                "task_ids": {
                    "type": "array",
                    "description": "List of task IDs to assign to the agent at creation time (optional). When present, each task must exist and be unassigned. Omit to create an idle agent — admins can assign tasks later via the Tasks page.",
                    "items": {"type": "string"},
                },
                "capabilities": {
                    "type": "array",
                    "description": "List of agent capabilities (e.g., 'code_edit', 'file_read')",
                    "items": {"type": "string"},
                    "default": [],
                },
                "prompt_template": {
                    "type": "string",
                    "description": "Prompt template to use ('worker_with_rag', 'basic_worker', 'frontend_worker', 'admin_agent', 'custom')",
                    "enum": [
                        "worker_with_rag",
                        "basic_worker",
                        "frontend_worker",
                        "admin_agent",
                        "custom",
                    ],
                    "default": "worker_with_rag",
                },
                "custom_prompt": {
                    "type": "string",
                    "description": "Custom prompt text (required if prompt_template is 'custom')",
                },
                "send_prompt": {
                    "type": "boolean",
                    "description": "Whether to automatically send the prompt to the tmux session after launch",
                    "default": True,
                },
                "prompt_delay": {
                    "type": "integer",
                    "description": "Seconds to wait before sending prompt (allows Claude to start up)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=create_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="view_status",
        description="View the status of all agents, connections, and the MCP server.",
        input_schema={  # From main.py:1663-1674
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended)."}
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_status_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="terminate_agent",
        description="Terminate an active agent with the given ID.",
        input_schema={  # From main.py:1676-1689
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the agent to terminate",
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=terminate_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="view_audit_log",
        description="View the in-memory audit log, optionally filtered by agent ID or action, with a limit.",
        input_schema={  # From main.py:1788-1810
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Filter audit log by agent ID (optional)",
                },
                "action": {
                    "type": "string",
                    "description": "Filter audit log by action (e.g., 'create_agent') (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 50, max 200)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_audit_log_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="get_agent_tokens",
        description="Retrieve agent tokens with advanced filtering capabilities. Supports filtering by status, agent_id pattern, creation date range, and more.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "filter_status": {
                    "type": "string",
                    "description": "Filter by agent status (e.g., 'active', 'terminated', 'created')",
                },
                "filter_agent_id_pattern": {
                    "type": "string",
                    "description": "Filter by agent ID using SQL LIKE pattern (e.g., 'test_%', '%prod%')",
                },
                "filter_created_after": {
                    "type": "string",
                    "description": "Filter agents created after this date (ISO format: YYYY-MM-DDTHH:MM:SS)",
                },
                "filter_created_before": {
                    "type": "string",
                    "description": "Filter agents created before this date (ISO format: YYYY-MM-DDTHH:MM:SS)",
                },
                "include_terminated": {
                    "type": "boolean",
                    "description": "Include terminated agents in results (default: false)",
                    "default": False,
                },
                "include_sensitive_data": {
                    "type": "boolean",
                    "description": "Include full tokens in response (default: true). If false, tokens will be masked for security.",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of agents to return (default: 50, max: 500)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500,
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of agents to skip for pagination (default: 0)",
                    "default": 0,
                    "minimum": 0,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Field to sort by (default: 'created_at')",
                    "enum": ["created_at", "updated_at", "agent_id", "status"],
                    "default": "created_at",
                },
                "sort_order": {
                    "type": "string",
                    "description": "Sort order (default: 'DESC')",
                    "enum": ["ASC", "DESC"],
                    "default": "DESC",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=get_agent_tokens_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="relaunch_agent",
        description="Relaunch an existing terminated/completed/failed/cancelled agent by reusing its tmux session.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "ID of the agent to relaunch",
                },
                "generate_new_token": {
                    "type": "boolean",
                    "description": "Generate a new token for the relaunched agent (default: false)",
                    "default": False,
                },
                "custom_prompt": {
                    "type": "string",
                    "description": "Custom prompt to send instead of template prompt",
                },
                "prompt_template": {
                    "type": "string",
                    "description": "Prompt template to use (default: 'worker_with_rag')",
                    "default": "worker_with_rag",
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=relaunch_agent_tool_impl,
        visibility="operator",
    )


# Call registration when this module is imported
register_admin_tools()
