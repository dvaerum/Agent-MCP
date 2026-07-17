# Agent-MCP/mcp_template/mcp_server_src/tools/agent_tools.py
from typing import List, Dict, Any, Optional

import mcp.types as mcp_types # Assuming this is your mcp.types path

from .registry import register_tool
from ..core.config import logger
from ..core.auth import get_agent_id # verify_token not strictly needed here
from ..core.authorize import requires_capability
from ..core.principal import Principal
from ..utils.audit_utils import log_audit
from ..utils.project_utils import generate_system_prompt # The core logic

# --- get_system_prompt tool ---
# Original logic from main.py: lines 1352-1384 (get_system_prompt_tool function)
# Wave 9 PR 2: @requires("any") → @requires_capability("mcp.connect").
# The system-prompt fetch is the fundamental "you can use the MCP wire"
# capability — every authenticated agent (worker / manager) carries
# ``mcp.connect`` via :data:`AGENT_ROLE_BUNDLES`, and sysadmin
# wildcards admit operator paths the same way the legacy "any" gate
# did via ``has_role("admin")``.
@requires_capability("mcp.connect")
async def get_system_prompt_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> List[mcp_types.TextContent]:
    # Identity + connection-snippet bearer come from the threaded
    # Principal: ``principal.agent_id`` is the caller's id and
    # ``principal.source_token`` is the same bearer the header back-fill
    # injects into ``arguments["token"]``. The ``arguments["token"]``
    # read below is a graceful fallback for direct-call tests that don't
    # thread a Principal; PR 2 retires it (token-retirement plan Phase A).
    if principal is not None:
        requesting_agent_id = principal.agent_id
        agent_token_for_prompt = principal.source_token
    else:
        agent_auth_token = arguments.get("token")  # This is the agent's own token
        requesting_agent_id = get_agent_id(agent_auth_token)
        agent_token_for_prompt = agent_auth_token

    # `generate_system_prompt` takes the agent_id and the agent's own
    # token (for the connection snippet); the "Admin" vs "Worker" label
    # is derived from the agent's ``agent_role`` column.
    system_prompt_str = generate_system_prompt(
        agent_id=requesting_agent_id,
        agent_token_for_prompt=agent_token_for_prompt,
    )

    log_audit(requesting_agent_id, "get_system_prompt", {}) # main.py:1375
    
    logger.info(f"Provided system prompt for agent '{requesting_agent_id}'.")
    return [mcp_types.TextContent(
        type="text",
        text=f"System Prompt for Agent '{requesting_agent_id}':\n\n{system_prompt_str}"
    )] # main.py:1377-1381


# --- Register agent-specific tools ---
def register_agent_tools():
    register_tool(
        name="get_system_prompt", # main.py:1773 (schema name)
        description="Get the tailored system prompt for the currently authenticated agent, including connection instructions.",
        input_schema={ # From main.py:1774-1786
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Agent authentication token (the agent's own token). Optional if Authorization: Bearer header is supplied (recommended)."}
            },
            "required": [],
            "additionalProperties": False
        },
        implementation=get_system_prompt_tool_impl
    )

# Call registration when this module is imported
register_agent_tools()