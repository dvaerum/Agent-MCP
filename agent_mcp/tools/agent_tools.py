# Agent-MCP/mcp_template/mcp_server_src/tools/agent_tools.py
from typing import Dict, Any, Optional

from .registry import register_tool
from ..core.config import logger
from ..core.authorize import requires_capability
from ..core.principal import Principal
from ..core.tool_result import Ok, ToolResult
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
) -> ToolResult:
    # Identity + connection-snippet bearer come from the threaded
    # Principal: ``principal.agent_id`` is the caller's id and
    # ``principal.source_token`` is the caller's own bearer. The
    # dispatcher always synthesizes a Principal from the
    # ``request_auth_token`` contextvar, so ``principal`` is never None
    # here (token-retirement plan Phase C — the legacy self-auth token
    # argument read is retired).
    requesting_agent_id = principal.agent_id
    agent_token_for_prompt = principal.source_token

    # `generate_system_prompt` takes the agent_id and the agent's own
    # token (for the connection snippet); the "Admin" vs "Worker" label
    # is derived from the agent's ``agent_role`` column.
    system_prompt_str = generate_system_prompt(
        agent_id=requesting_agent_id,
        agent_token_for_prompt=agent_token_for_prompt,
    )

    log_audit(requesting_agent_id, "get_system_prompt", {}) # main.py:1375
    
    logger.info(f"Provided system prompt for agent '{requesting_agent_id}'.")
    # Wave-6 typed result: every tool impl must return a ``ToolResult``.
    # Post-PR-6 the ``list[TextContent]`` auto-wrap bridge in the
    # dispatcher is gone — a bare list return is rejected as ``Failed``
    # ("unexpected type list"). ``Ok(message=...)`` renders to the same
    # single TextContent block on the MCP wire (render_as_text_content).
    return Ok(
        message=(
            f"System Prompt for Agent '{requesting_agent_id}':"
            f"\n\n{system_prompt_str}"
        )
    )


# --- Register agent-specific tools ---
def register_agent_tools():
    register_tool(
        name="get_system_prompt", # main.py:1773 (schema name)
        description="Get the tailored system prompt for the currently authenticated agent, including connection instructions.",
        input_schema={ # From main.py:1774-1786
            "type": "object",
            "properties": {
            },
            "required": [],
            "additionalProperties": False
        },
        implementation=get_system_prompt_tool_impl
    )

# Call registration when this module is imported
register_agent_tools()