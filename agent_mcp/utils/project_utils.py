# Agent-MCP/agent-mcp/utils/project_utils.py
import os
import json
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..core import globals as g
from ..core.config import (
    logger,
    get_project_dir,
)  # Import get_project_dir for MCP_VERSION if needed

# __version__ was in mcp_template/__init__.py.
# If MCP_VERSION is needed here, it should ideally be sourced from a single place.
# For now, let's assume it might come from the main package's __init__ or a dedicated version file.
# We can hardcode it temporarily or make it configurable.
try:
    # Attempt to get version from the root __init__.py of agent-mcp
    from agent_mcp import __version__ as MCP_VERSION
except ImportError:
    logger.warning(
        "Could not import __version__ from agent_mcp. Using default '0.1.0'."
    )
    MCP_VERSION = "0.1.0"  # Fallback, matches original main.py:1041


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

    # Create initial config file if it doesn't exist
    # Original main.py lines 902-914
    config_path = agent_dir / "config.json"
    if not config_path.exists():
        # Wave 3 (prancy-napping-pie): renamed the field from
        # ``admin_token`` to ``system_token`` so downstream consumers
        # (legacy CLI, deploy scripts) that still read the file get a
        # ``KeyError`` rather than silently consuming stale data when
        # the rename happens. ``g.system_token`` is the canonical
        # storage; ``g.admin_token`` aliases to it.
        current_system_token = g.system_token

        config_data = {
            "project_name": project_path.name,
            "created_at": datetime.datetime.now().isoformat(),
            "system_token": current_system_token,
            "mcp_version": MCP_VERSION,
        }
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to write initial config.json to {config_path}: {e}")
            return None  # Indicate failure
        except Exception as e:
            logger.error(
                f"Unexpected error writing initial config.json: {e}", exc_info=True
            )
            return None

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
    from ..core.repositories import agent_repo

    working_dir = agent_repo.get_working_directory(agent_id) or os.getcwd()

    # Base prompt content from original main.py lines 1208-1224
    base_prompt = f"""You are an AI agent running in Cursor, connected to a Multi-Agent Collaboration Protocol (MCP) server.

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
    agent_row = agent_repo.get_agent_by_id(agent_id) or {}
    agent_role = agent_row.get("agent_role", "worker")
    agent_type = "Admin" if agent_role == "manager" else "Worker"

    agent_details_str = f"""Agent ID: {agent_id}
Agent Type: {agent_type}
"""

    # Connection code snippet for the agent to use
    # (Original main.py lines 1229-1237 for connection code structure)
    # The agent's connection example uses the Streamable HTTP /mcp
    # endpoint (spec rev 2025-03-26); the old /messages/ paired-endpoint
    # transport was removed (see main_app.py:_MIGRATION_BODY).
    #
    # Pre-v5.0.53 this honoured an MCP_SERVER_URL env override, but
    # nothing else in the codebase reads that variable — leaving the
    # override in place would let a stray export inject an attacker-
    # controlled URL into every worker's system prompt. The PORT env
    # var (which the server itself binds to) is the only knob.
    mcp_server_url_for_client = (
        f"http://localhost:{os.environ.get('PORT', '8080')}/mcp"
    )

    # The original connection code snippet in main.py was quite extensive and specific.
    # For 1-to-1, we should replicate that structure.
    # It assumed the agent would use 'requests' and provided a `call_mcp_tool` like function.
    # The token used in HEADERS should be `agent_token_for_prompt`.
    connection_code_lines = [
        f'MCP_SERVER_URL = "{mcp_server_url_for_client}"  # Adjust if your server\'s tool endpoint is different',
        f'AGENT_TOKEN = "{agent_token_for_prompt}" # This is your unique agent token',
        "",
        "HEADERS = {",
        '    "Content-Type": "application/json",',
        # The original prompt had a complex way of deciding which token to use in Authorization.
        # It should always be the AGENT_TOKEN for the agent's own calls.
        '    "Authorization": f"Bearer {{AGENT_TOKEN}}"',
        "}",
        "",
        "def call_mcp_tool(tool_name: str, arguments: dict) -> dict:",
        "    payload = {",
        '        "id": f"call_{{requests.compat.urlencode(arguments)[:10]}}", # Example unique ID',
        '        "type": "tool_call",',
        '        "tool": tool_name,',
        '        "arguments": arguments',
        "    }",
        '    print(f"Calling tool: {{tool_name}} with args: {{arguments}}") # Debug print',
        "    try:",
        "        response = requests.post(MCP_SERVER_URL, headers=HEADERS, json=payload, timeout=60)",
        "        response.raise_for_status()  # Raise an HTTPError for bad responses (4XX or 5XX)",
        "        # The MCP server is expected to return a list of content blocks.",
        "        # We need to parse the 'text' from the first TextContent block.",
        "        response_data = response.json()",
        "        if isinstance(response_data, list) and len(response_data) > 0:",
        "            first_item = response_data[0]",
        "            if isinstance(first_item, dict) and first_item.get('type') == 'text':",
        "                return {{'text_response': first_item.get('text', '')}}",
        "        return {{'raw_response': response_data}} # Fallback",
        "    except requests.exceptions.Timeout:",
        '        print(f"Timeout calling MCP tool {{tool_name}}")',
        "        return {{'error': 'Timeout'}}"
        "    except requests.exceptions.RequestException as e:",
        '        print(f"Error calling MCP tool {{tool_name}}: {{e}}")',
        "        if e.response is not None:",
        '            print(f"Response content: {{e.response.text}}")',
        "            return {{'error': str(e), 'response_text': e.response.text}}",
        "        return {{'error': str(e)}}",
        "",
        "# Example usage:",
        '# result = call_mcp_tool("view_tasks", {{"token": AGENT_TOKEN}})',
        "# if result and 'error' not in result: print(json.dumps(result, indent=2))",
    ]
    connection_code_str = "\n".join(connection_code_lines)

    # Construct full prompt (Original main.py line 1238)
    full_prompt = (
        base_prompt
        + agent_details_str
        + "\nCopy-paste this Python code into your environment to connect and interact with the MCP server:\n"
        + "```python\nimport requests\nimport json\n\n"
        + connection_code_str
        + "\n```\n\n"
        + "Use the available tools (the server will list them or consult documentation) via `call_mcp_tool` to manage your work."
    )
    return full_prompt
