# Agent-MCP/mcp_template/mcp_server_src/tools/utility_tools.py
from typing import Dict, Any, Optional

from .registry import register_tool
from ..core.config import logger
from ..core.principal import Principal
from ..core.tool_result import Ok, ToolResult


# --- test_tool ---
# Original logic from main.py: lines 1933-1938 (test_tool function)
# This tool did not take any arguments in the original `handle_tool` dispatch.
async def test_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Simple test tool that returns a success message.
    It does not require any arguments.
    """
    # `arguments` + ``principal`` are present for dispatcher
    # consistency, but not used.
    logger.info("Executing test_tool_impl.")
    return Ok(message="Tool is working!")


# --- Register utility tools ---
def register_utility_tools():
    # The original `list_tools` in main.py did not explicitly list a "test" tool schema.
    # It was handled as a special case in `handle_tool` (main.py:1879).
    # For a consistent registry, we should define a schema for it, even if it's empty.
    register_tool(
        name="test",
        description="A simple test tool to verify the tool calling mechanism is working.",
        input_schema={ # Define an empty input schema
            "type": "object",
            "properties": {}, # No properties
            "additionalProperties": False
        },
        implementation=test_tool_impl
    )

# Call registration when this module is imported
register_utility_tools()