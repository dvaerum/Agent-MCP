# Agent-MCP/agent-mcp/utils/project_utils.py
import os
import json
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..core.config import (
    logger,
    get_project_dir,
)


# Original location: main.py lines 876-929 (init_agent_directory)
def init_agent_directory(project_dir_str: str) -> Optional[Path]:
    """
    Initialize the .agent directory structure in the specified project directory.
    If the directory structure already exists, it verifies it.
    Original main.py: lines 876-929
    """
    try:
        project_path = Path(project_dir_str).resolve()
    except Exception as e:
        logger.error(f"Invalid project directory string '{project_dir_str}': {e}")
        return None

    # Validate that the project directory is not the MCP directory itself
    # This logic needs to correctly identify the MCP codebase root.
    # Assuming this file is at: Agent-MCP/agent-mcp/utils/project_utils.py
    # Then, __file__.resolve() gives the path to this file.
    # .parent -> .../utils
    # .parent.parent -> .../mcp_server_src
    # .parent.parent.parent -> .../agent-mcp (This is the root of the agent code package)
    # .parent.parent.parent.parent -> .../Agent-MCP (This is the repository root)
    # The original check was against `Path(__file__).resolve().parent.parent` from `main.py`
    # which would be `agent-mcp`.
    agent_mcp_codebase_root_for_check = (
        Path(__file__).resolve().parent.parent.parent
    )  # This should point to agent-mcp

    # Original main.py line 880-884
    if (
        project_path == agent_mcp_codebase_root_for_check
        or project_path in agent_mcp_codebase_root_for_check.parents
    ):
        # This warning matches the original behavior.
        logger.warning(
            f"WARNING: Initializing .agent in the MCP directory itself ({project_path}) or its parent is not recommended!"
        )
        logger.warning(
            f"Please specify a project directory that is NOT the MCP codebase."
        )
        # Original code proceeded with a warning, so we do the same.

    agent_dir = project_path / ".agent"

    # Original main.py lines 887-899 (directory list)
    directories_to_create = [
        "",  # Ensures .agent itself is created
        "logs",
        "diffs",
        "notifications",
        "notifications/pending",
        "notifications/acknowledged",
    ]

    try:
        for directory_suffix in directories_to_create:
            (agent_dir / directory_suffix).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"Failed to create .agent directory structure in {agent_dir}: {e}")
        return None  # Indicate failure

    # Create initial daily logs file if it doesn't exist
    # Original main.py lines 917-926
    log_file_dir = agent_dir / "logs"
    # log_file_dir.mkdir(parents=True, exist_ok=True) # Ensured by directories_to_create
    log_file_path = log_file_dir / f"{datetime.date.today().isoformat()}.json"
    if not log_file_path.exists():
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "event": "agent_directory_initialized",
            "details": "Initial setup of .agent directory",
        }
        try:
            with open(log_file_path, "w", encoding="utf-8") as f:
                json.dump(
                    [log_entry], f, indent=2
                )  # Original stored a list with one entry
        except IOError as e:
            logger.error(
                f"Failed to write initial daily log file to {log_file_path}: {e}"
            )
            # Continue, as this is less critical than config.json, matching original behavior.
        except Exception as e:
            logger.error(
                f"Unexpected error writing initial daily log file: {e}", exc_info=True
            )

    logger.info(f".agent directory structure initialized/verified in {agent_dir}")
    return agent_dir


# Original location: main.py lines 1206-1239 (generate_system_prompt)
def generate_system_prompt(
    agent_id: str, agent_token_for_prompt: str
) -> str:
    """
    Generate a system prompt for an agent.
    Original main.py: lines 1206-1239.
    PR-W2c: routed through AgentRepository.get_working_directory() so
    a cache miss falls through to the DB row instead of dropping to
    CWD silently.

    Wave 3 (prancy-napping-pie) dropped the ``admin_token_runtime``
    parameter. The "Admin" / "Worker" label in the rendered prompt
    now comes from the agent's ``agent_role`` column ('manager' or
    'worker') rather than a token-equality comparison.
    """
    # Determine working directory for the prompt
    # Fallback to CWD if agent_id is unknown to the repo, though it
    # should be known by the time this is called.
    from ..repositories import agent_repo

    working_dir = agent_repo.get_working_directory(agent_id) or os.getcwd()

    # Base prompt content from original main.py lines 1208-1224
    base_prompt = f"""You are an AI agent connected to a Multi-Agent Collaboration Protocol (MCP) server.

Your goal is to complete tasks efficiently and collaboratively using a shared, persistent knowledge base.

**Core Responsibilities & Tools:**
*   **File Safety:** Before modifying any file, use `check_file_status` to see if another agent is using it. Use `update_file_status` to claim files ('editing', 'reading', 'reviewing') before you start and 'released' when done.
*   **Task Management:** Use `view_tasks` to see your assigned tasks (filter by agent ID or status). Update progress with `update_task_status`. If a task is complex, use `request_assistance` or `create_self_task`.
*   **Project Context (Key-Value):** 
    *   Use `view_project_context` with `context_key` for specific values (e.g., API endpoints, configuration) or `search_query` to find relevant keys via keywords.
    *   (Admin) Use `update_project_context` to add/modify precise key-value context.
*   **File Metadata:** 
    *   Use `view_file_metadata` (with `filepath`) to understand a file's purpose, components, etc.
    *   (Admin) Use `update_file_metadata` to add/update structured information about specific files.
*   **RAG Querying:** Use `ask_project_rag` with a natural language `query` to ask broader questions about the project. The system will search across documentation, context, and metadata to synthesize an answer. (Index updates automatically in the background).
*   **Event-Driven Loop (preferred over polling):** Use `wait_for_events` to long-poll for new direct messages, broadcasts, and task assignments / changes addressed to you. Default 60s timeout, server caps at 900s. Pass the previous response's `next_cursor` as `since` on each call to advance through the timeline. Replaces the old `view_tasks` + `get_agent_messages` polling pattern — your work loop becomes "wait, handle event(s), wait" instead of "sleep, poll, sleep". For richer MCP clients, the same data is exposed as standard MCP **resources** at `agent-mcp://inbox/<your_agent_id>` (event timeline) and `agent-mcp://status/<your_agent_id>` (ambient counters: `unread_messages`, `unfinished_tasks`).
*   **Parallelization:** Analyze tasks for opportunities to work in parallel. Break down large tasks into smaller sub-tasks. Clearly define dependencies.
*   **Auditability:** Log all significant actions for tracking and debugging.

Your working directory is: {working_dir}
"""

    # Determine agent type for the prompt.
    # Wave 3 (prancy-napping-pie): label comes from the agent's
    # ``agent_role`` column ('worker' or 'manager') instead of a
    # token-equality comparison against the system bearer. Manager
    # agents render as "Admin" in the prompt for back-compat with the
    # pre-Wave-3 wording — Wave 4 may revisit this label vocabulary.
    agent_row = agent_repo.get_by_id(agent_id) or {}
    agent_role = agent_row.get("agent_role", "worker")
    agent_type = "Admin" if agent_role == "manager" else "Worker"

    agent_details_str = f"""Agent ID: {agent_id}
Agent Type: {agent_type}
"""

    # Tool-access note.
    #
    # This prompt is delivered to an agent that is ALREADY connected to
    # this server through a real MCP client (a spawned/registered worker).
    # The client owns the wire protocol — JSON-RPC framing, transport, and
    # the bearer credential are all handled by the client, not by anything
    # the agent hand-writes.
    #
    # Historically this block baked a ~40-line hand-rolled ``call_mcp_tool``
    # snippet (a ``requests.post`` of a ``{"type": "tool_call", ...}`` body
    # to a ``/mcp`` endpoint) and told the agent it was "running in Cursor".
    # That protocol is FICTIONAL — it is not how MCP works — so every worker
    # received wrong tool-calling instructions in its live system prompt.
    # Re-teaching a (wrong) protocol in the system prompt is both incorrect
    # and unnecessary: the correct move is to point the agent at the tools it
    # can call and let its MCP client do the talking. ``agent_token_for_prompt``
    # is kept in the signature for call-site compatibility but is no longer
    # echoed into the prompt — the client already holds the agent's credential.
    tool_access_note = (
        "**Tool access:** Your tools are available directly through your "
        "MCP connection. The MCP client handles the protocol (transport, "
        "framing, and authentication) for you, so call each tool by name "
        "(for example `view_tasks`, `update_task_status`, `ask_project_rag`) "
        "the same way you use any other tool — you do not need to build HTTP "
        "requests or manage tokens yourself. Consult your client's tool "
        "listing to discover the full set of tools available to you."
    )

    # Manager-only coordination block.
    #
    # A manager is a Claude Code process connected via MCP that coordinates
    # worker teammates (other MCP-connected Claude Code processes). Two
    # things bit a live manager and belong in its system prompt:
    #
    # 1. Messaging teammates. Claude Code's NATIVE ``SendMessage`` tool only
    #    reaches native Task-spawned subagents in the same session — it does
    #    NOT reach MCP teammates. A manager that used it saw every send fail
    #    silently. The correct tool is the agent-mcp ``send_agent_message``
    #    MCP tool, addressed by ``recipient_id`` = the teammate's agent_id.
    # 2. Working folder. The manager's own repo/checkout is its personal
    #    space for the notes / progress / status it keeps while coordinating.
    manager_note = ""
    if agent_role == "manager":
        manager_note = (
            "\n\n"
            "**Coordinating teammates (messaging):** To message another "
            "agent, use the agent-mcp `send_agent_message` tool with "
            "`recipient_id` set to the teammate's agent_id exactly as listed "
            "(for example `pikvm-mcp-server@nixos-developer-system`). Do NOT "
            "use Claude Code's native `SendMessage` tool to reach teammates "
            "— that only reaches native Task-spawned subagents inside your "
            "own session, not the MCP teammates coordinated through this "
            "server, so those sends silently fail. If an agent_id is shown "
            "with a leading `@` (an @-mention prefix in the UI), drop the "
            "`@` — it is not part of the agent_id."
            "\n\n"
            "**Your working folder:** Your working directory (above) is your "
            "own repo/checkout — your personal space for coordination. Keep "
            "your own notes, progress reports, status logs, and scratch work "
            "there as you track the work across your teammates. It is where "
            "you record and follow your own progress."
        )

    # Construct full prompt (Original main.py line 1238)
    full_prompt = (
        base_prompt + agent_details_str + "\n" + tool_access_note + manager_note
    )
    return full_prompt
