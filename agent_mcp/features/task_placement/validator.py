# Task placement validator using RAG system
import json
from typing import Optional, List, Dict, Any
from ...tools.rag_tools import ask_project_rag_tool_impl
from ...core.principal import Principal
import mcp.types as mcp_types
from ...core.config import logger, TASK_ANALYSIS_MAX_TOKENS

async def validate_task_placement(
    title: str,
    description: str,
    parent_task_id: Optional[str],
    depends_on_tasks: Optional[List[str]],
    created_by: str,
    principal: Optional[Principal] = None,
    requesting_agent_id: Optional[str] = None,
    can_view_all_tasks: bool = True,
    include_foreign: bool = False,
) -> Dict[str, Any]:
    """
    Advisory-only task placement analysis via the RAG system.

    Suggests a parent task, suggests dependency changes, and flags
    likely duplicate tasks by semantic similarity over the RAG corpus.

    This function does NOT touch the database and does NOT enforce the
    single-root-task invariant. That invariant is a hard structural
    constraint (``COUNT(*) ... WHERE parent_task IS NULL``) enforced in
    ``task_tools.py`` *before* this validator is ever called — a
    features-layer module has no business opening its own DB
    connection, and an LLM has no way to verify a hierarchy claim
    against ground truth, so it must never be trusted to enforce one
    (arch-r4 #9). This function only suggests; it never denies a task
    based on hierarchy.

    Args:
        title: Proposed task title
        description: Proposed task description
        parent_task_id: Proposed parent task ID (if any)
        depends_on_tasks: List of proposed dependency task IDs
        created_by: Agent ID recorded as the task's author. NOTE: this is
            the authorship field, NOT necessarily the RAG-scope identity —
            the ``assign_task`` path authors as ``"admin"`` while the real
            caller may be a worker, so ``requesting_agent_id`` is threaded
            separately for the ownership scope.
        principal: The caller's threaded Principal, forwarded to the
            ImportError-fallback RAG call (``ask_project_rag_tool_impl``
            is principal-authed). The primary path
            (``query_rag_system_with_model``) needs no auth token — it
            scopes on ``requesting_agent_id`` + ``can_view_all_tasks``.
        requesting_agent_id: The agent_id whose ``view_tasks`` visibility
            scopes the RAG duplicate-check search (R5-F1). Defaults to
            ``created_by`` when not given — on the ``create_self_task``
            path the author IS the caller, so the two coincide. The
            ``assign_task`` path passes the real caller explicitly because
            it authors tasks as ``"admin"``; scoping to ``"admin"`` there
            for a fell-through worker would instead expose admin's tasks.
        can_view_all_tasks: Whether the caller holds ``tasks.assign``
            (operator / manager / sysadmin). Threaded into
            ``query_rag_system_with_model`` so a non-privileged worker's
            placement analysis is ownership-scoped to its own tasks —
            search must not disclose a task the caller couldn't read
            directly via ``view_tasks`` (R5-F1, the R4-F4 sibling on this
            RAG entry point). Defaults ``True`` (unscoped) to match the
            fallback semantics of both RAG entry points.
        include_foreign: ``config_allow_worker_view_foreign_tasks``
            (schema default ``True``; defaults ``False`` HERE, mirroring
            ``query_rag_system_with_model``, so a caller that doesn't
            pass it keeps the old exact-match-only scope). Resolved by
            the caller (``task_tools.py``, alongside ``can_view_all_
            tasks``) rather than read here — this module stays a
            features-layer client of ``tools.rag_tools``/``rag.query``,
            not a reader of the ``tools/access.py`` config store.

    Returns:
        Dictionary with validation results:
        {
            "status": "approved" | "suggest_changes" | "warning" | "denied",
            "suggestions": {
                "parent_task": suggested_parent_task_id,
                "dependencies": [suggested_dep_ids],
                "reasoning": "Explanation for suggestions"
            },
            "duplicates": [{
                "task_id": existing_task_id,
                "similarity": 0.0-1.0,
                "title": existing_title
            }],
            "message": "Human-readable message"
        }
    """
    try:
        # Format the query for RAG with emphasis on critical thinking
        query = f"""
        CRITICAL THINKING REQUIRED: Analyze the proposed task placement with deep consideration of the existing task structure:

        Title: {title}
        Description: {description}
        Proposed Parent Task: {parent_task_id or 'None (root-level task)'}
        Proposed Dependencies: {json.dumps(depends_on_tasks or [])}
        Created By: {created_by}

        YOU MUST CRITICALLY EVALUATE:

        1. LOGICAL PLACEMENT:
           - Analyze ALL existing tasks to find the most logical parent
           - Consider the task's purpose, scope, and relationship to other tasks
           - Don't just accept the proposed parent - think if there's a better one

        2. DEPENDENCIES:
           - Identify ALL tasks this should depend on based on logical workflow
           - Consider both direct and indirect dependencies
           - Remove any redundant or incorrect dependencies

        3. DUPLICATION:
           - Check if similar tasks already exist
           - Consider if this should be a subtask of an existing task instead

        4. PROJECT STRUCTURE:
           - Ensure the task fits logically within the project's architecture
           - Consider the impact on the overall task hierarchy

        Please respond in the following JSON format:
        {{
            "placement_assessment": "appropriate" | "needs_adjustment" | "problematic",
            "parent_suggestion": {{
                "recommended_parent": "task_id or null",
                "reasoning": "detailed explanation of why this parent is the most logical choice after analyzing all tasks"
            }},
            "dependency_suggestions": {{
                "add_dependencies": ["task_id1", "task_id2"],
                "remove_dependencies": ["task_id3"],
                "reasoning": "detailed explanation of the dependency logic"
            }},
            "duplication_check": {{
                "similar_tasks": [
                    {{
                        "task_id": "existing_task_id",
                        "title": "existing task title",
                        "similarity": 0.85,
                        "reasoning": "why they are similar"
                    }}
                ],
                "is_duplicate": true | false
            }},
            "critical_thinking_summary": "Your detailed analysis of how this task fits into the overall project structure",
            "overall_recommendation": "proceed" | "modify" | "reconsider" | "deny",
            "message": "Human-readable explanation of the assessment"
        }}
        """
        
        # For task analysis, use the cheaper model directly instead of the full RAG system
        # This allows us to use a different model for task placement analysis
        try:
            from ...features.rag.query import query_rag_system_with_model
        except ImportError:
            # If the function doesn't exist yet, fall back to regular RAG.
            # ``ask_project_rag_tool_impl`` authorizes off the Principal
            # (not a token arg), so thread the caller's Principal through.
            rag_response = await ask_project_rag_tool_impl(
                {"query": query},
                principal=principal,
            )
        else:
            # v5.0.44: model selection moved into completion_service;
            # the ``model_name`` parameter is now informational only.
            # SECURITY (R5-F1): the RAG scope keys on the REAL caller, not
            # the authorship field. Falls back to ``created_by`` only when
            # no explicit ``requesting_agent_id`` is supplied (the
            # create_self_task path, where they coincide).
            rag_requesting_agent_id = (
                requesting_agent_id
                if requesting_agent_id is not None
                else created_by
            )
            response_text = await query_rag_system_with_model(
                query_text=query,
                max_tokens=TASK_ANALYSIS_MAX_TOKENS,
                # SECURITY (R5-F1): thread the caller's task visibility so
                # the placement analysis can't surface a task the worker
                # couldn't read directly via ``view_tasks``.
                requesting_agent_id=rag_requesting_agent_id,
                can_view_all_tasks=can_view_all_tasks,
                include_foreign=include_foreign,
            )
            rag_response = [mcp_types.TextContent(type="text", text=response_text)]
        
        # Extract the text from the response
        response_text = rag_response[0].text if rag_response else ""
        
        # Check for "no knowledge" case
        if "no relevant context found" in response_text.lower() or "no knowledge" in response_text.lower():
            logger.info("RAG system has no task knowledge - recommending initial context setup")
            return {
                "status": "suggest_changes",
                "suggestions": {
                    "parent_task": None,  # Root level for initial task
                    "dependencies": [],
                    "reasoning": "No existing task hierarchy found. This should be a root-level task to establish the project structure."
                },
                "duplicates": [],
                "message": "No existing task knowledge found. Recommend creating as root task and adding project context/MCD."
            }
        
        # Try to parse JSON from the response
        try:
            # Look for JSON in the response (it might be wrapped in other text)
            json_start = response_text.find('{')
            json_end = response_text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                rag_data = json.loads(json_str)
            else:
                # Fallback if no JSON found
                rag_data = None
        except json.JSONDecodeError:
            logger.warning(f"Could not parse JSON from RAG response: {response_text[:200]}...")
            rag_data = None
        
        # Process the RAG response into our format
        if rag_data:
            # Map RAG recommendations to our status codes
            status_map = {
                "proceed": "approved",
                "modify": "suggest_changes",
                "reconsider": "warning",
                "deny": "denied"
            }

            status = status_map.get(
                rag_data.get("overall_recommendation", "proceed"),
                "approved"
            )

            # Extract suggestions
            parent_suggestion = rag_data.get("parent_suggestion", {})
            dependency_suggestions = rag_data.get("dependency_suggestions", {})
            
            suggestions = {
                "parent_task": parent_suggestion.get("recommended_parent"),
                "dependencies": depends_on_tasks or []
            }
            
            # Apply dependency modifications
            if dependency_suggestions.get("add_dependencies"):
                suggestions["dependencies"].extend(
                    dependency_suggestions["add_dependencies"]
                )
            if dependency_suggestions.get("remove_dependencies"):
                suggestions["dependencies"] = [
                    d for d in suggestions["dependencies"]
                    if d not in dependency_suggestions["remove_dependencies"]
                ]
            
            # Remove duplicates and None values from dependencies
            suggestions["dependencies"] = list(filter(None, set(suggestions["dependencies"])))
            
            # Add reasoning
            reasoning_parts = []
            if parent_suggestion.get("reasoning"):
                reasoning_parts.append(f"Parent: {parent_suggestion['reasoning']}")
            if dependency_suggestions.get("reasoning"):
                reasoning_parts.append(f"Dependencies: {dependency_suggestions['reasoning']}")
            
            suggestions["reasoning"] = " | ".join(reasoning_parts) if reasoning_parts else None
            
            # Extract duplicate information
            duplication_info = rag_data.get("duplication_check", {})
            duplicates = []
            for similar_task in duplication_info.get("similar_tasks", []):
                duplicates.append({
                    "task_id": similar_task.get("task_id"),
                    "similarity": similar_task.get("similarity", 0.0),
                    "title": similar_task.get("title", "Unknown")
                })
            
            # Include critical thinking summary in message
            critical_thinking = rag_data.get("critical_thinking_summary", "")
            base_message = rag_data.get("message", "Task placement validated via RAG")
            full_message = f"{base_message}\n\nCritical Analysis: {critical_thinking}" if critical_thinking else base_message
            
            return {
                "status": status,
                "suggestions": suggestions,
                "duplicates": duplicates,
                "message": full_message,
            }
        else:
            # Fallback response if RAG parsing failed
            logger.warning("RAG response parsing failed, using fallback approval")
            return {
                "status": "approved",
                "suggestions": {
                    "parent_task": parent_task_id,
                    "dependencies": depends_on_tasks or [],
                    "reasoning": None
                },
                "duplicates": [],
                "message": "RAG validation unavailable, proceeding with original placement"
            }
            
    except Exception as e:
        logger.error(f"Error validating task placement: {e}", exc_info=True)
        # Return a safe default that allows task creation
        return {
            "status": "approved",
            "suggestions": {
                "parent_task": parent_task_id,
                "dependencies": depends_on_tasks or [],
                "reasoning": None
            },
            "duplicates": [],
            "message": f"Validation error: {str(e)}. Proceeding with original placement."
        }