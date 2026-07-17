# Agent-MCP/mcp_template/mcp_server_src/tools/rag_tools.py
"""RAG-query MCP tool surface.

Wave 6 PR 1 migrated the lone tool here (`ask_project_rag`) to the
Principal + ToolResult signature. The legacy ``@requires("any")``
decorator is gone — the tool itself rejects non-agent principals via
``principal.kind == "agent_bearer"``, matching the pre-migration
admission ("active agent token required"). Operator-session callers
(dashboard) are still rejected because no current call site needs the
widening; PR 6 (or a later UX-driven PR) can broaden if needed.
"""

from typing import Any, Dict, Optional

from .registry import register_tool
from ..core.config import logger
from ..core.principal import Principal
from ..core.tool_result import Failed, Invalid, Ok, PermissionDenied, ToolResult
from ..utils.audit_utils import log_audit
# Import the core RAG querying logic
from ..features.rag.query import RAG_ERROR_SENTINELS, query_rag_system


# --- ask_project_rag tool ---
async def ask_project_rag_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    # SEC Wave-B (Finding 2): gate on the ``rag.query`` capability, not
    # the bare ``kind``. The prior ``kind == "agent_bearer"`` check
    # admitted a bearer whose ``agent_role`` is None (empty capability
    # bundle) — a token that carries no caps could still read the RAG
    # corpus. The ``kind`` check is retained so operators (who DO carry
    # ``rag.query`` in their project bundle) stay rejected — this tool
    # is agent-only by design; a later UX PR can widen if needed.
    if (
        principal is None
        or principal.kind != "agent_bearer"
        or not principal.has_capability("rag.query")
    ):
        return PermissionDenied(
            reason="agent token with rag.query capability required to query project RAG"
        )

    query_text = arguments.get("query")
    if not query_text or not isinstance(query_text, str):
        return Invalid(
            field="query",
            message="query text is required and must be a string.",
        )

    requesting_agent_id = principal.agent_id or ""
    log_audit(requesting_agent_id, "ask_project_rag", {"query": query_text})

    logger.info(
        f"Agent '{requesting_agent_id}' is asking project RAG: "
        f"'{query_text[:100]}...'"
    )

    # SECURITY (R4-F4): thread the caller's task-visibility into the RAG
    # query so search can't surface a task the caller couldn't read
    # directly via ``view_tasks``. ``tasks.assign`` is the supervision-
    # tier marker (operator / manager / sysadmin) that ``view_tasks``
    # uses to grant the all-tasks view; a worker lacks it and is scoped
    # to its own assigned tasks.
    can_view_all_tasks = principal.has_capability("tasks.assign")

    try:
        # query_rag_system handles its own errors and always returns a
        # string — but on a provider/DB/config FAILURE that string is
        # category-only error PROSE (a sentinel from
        # ``RAG_ERROR_SENTINELS``), not an answer. Wrapping it in ``Ok``
        # regardless of success is the worker-msg bug: a genuine outage
        # reached the worker as a SUCCESS envelope whose text merely
        # started with "Error:", so the worker treated the outage as a
        # factual answer or filed a false bug. Detect the sentinels and
        # surface a ``Failed`` (isError=True / HTTP 500) so the failure
        # is classified as a failure. A genuine "no relevant information"
        # answer is NOT a sentinel and stays a success.
        answer_text = await query_rag_system(
            query_text,
            requesting_agent_id=requesting_agent_id,
            can_view_all_tasks=can_view_all_tasks,
        )
        if answer_text in RAG_ERROR_SENTINELS:
            logger.warning(
                "ask_project_rag: query_rag_system returned an error "
                "sentinel for agent '%s'; surfacing as Failed (detail "
                "already logged server-side by the query layer).",
                requesting_agent_id,
            )
            # SD-R9-1: static, category-only message — no provider names,
            # URLs, or exception text (the query layer already logged the
            # detail with exc_info).
            return Failed(
                message=(
                    "RAG is temporarily unavailable (provider or index "
                    "error); retry shortly, or ask an operator to check "
                    "RAG configuration."
                )
            )
        # data carries the same text so REST consumers can read it
        # programmatically too.
        return Ok(data={"answer": answer_text}, message=answer_text)
    except Exception as e:
        # Defensive — query_rag_system catches its own errors; this
        # arm only fires for unexpected exceptions in the wrapper.
        logger.error(
            f"Unexpected error in ask_project_rag_tool_impl for agent "
            f"'{requesting_agent_id}': {e}",
            exc_info=True,
        )
        return Failed(
            message=(
                f"An unexpected error occurred while processing your RAG "
                f"query: {e}"
            )
        )


# --- Register RAG tools ---
def register_rag_tools():
    register_tool(
        name="ask_project_rag", # main.py:1869 (schema name)
        description="Ask a natural language question about the project. The system uses RAG (Retrieval Augmented Generation) to find relevant information from indexed documentation, context, and metadata to synthesize an answer.",
        input_schema={ # From main.py:1870-1881
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The natural language question to ask about the project."}
            },
            "required": ["query"],
            "additionalProperties": False
        },
        implementation=ask_project_rag_tool_impl
    )

# Call registration when this module is imported
register_rag_tools()
