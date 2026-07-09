# Agent-MCP/mcp_template/mcp_server_src/features/rag/query.py
import sqlite3  # For type hinting and error handling
from typing import List, Dict, Any, Optional

# Imports from our project
from ...core.config import (
    logger,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSION,
    MAX_CONTEXT_TOKENS,  # From main.py:182
)
from ...db.connection import get_db_connection, is_vss_loadable
from ...external.openai_service import get_openai_client
from ...external.completion_service import (
    CompletionConfigError,
    completion_client,
)

# For OpenAI exceptions
import openai


def _is_secret_key(key: Optional[str]) -> bool:
    """Thin lazy wrapper over the canonical
    :func:`agent_mcp.tools.project_context_tools.is_secret_key`.

    Imported lazily (not at module top) to sidestep the import cycle:
    ``tools/__init__`` imports ``rag_tools`` which imports this module,
    so a top-level ``from ...tools...`` here can hit a half-initialised
    tools package. The RAG side-channel must apply the SAME secret-key
    policy as ``view_project_context`` so a worker can't read a
    ``config_*_token`` value by asking ``ask_project_rag`` — see
    ``tests/test_sec_rag_secret_redaction.py``.
    """
    from ...tools.project_context_tools import is_secret_key

    return is_secret_key(key)


# Canonical embedded-secret VALUE scanner, reused (not duplicated) from
# the index path so the live-context query filter applies the SAME skip
# the indexer does: a secret in the VALUE of a non-secret-named key
# (e.g. ``deploy_notes`` holding an AWS key) must not reach the LLM.
from .indexing import _value_has_embedded_secret  # noqa: E402


def _drop_secret_context_chunks(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop retrieved vector chunks that carry a secret project_context
    row. Defense against a STALE index that embedded a secret before the
    index-time skip (indexing.py) was in place: the chunk's
    ``source_ref`` is the context_key for ``source_type == "context"``.
    """
    return [
        r
        for r in results
        if not (
            r.get("source_type") == "context"
            and _is_secret_key(r.get("source_ref"))
        )
    ]


# Original location: main.py lines 1432 - 1566 (ask_project_rag_tool function body)


async def query_rag_system(query_text: str) -> str:
    """
    Processes a natural language query using the RAG system.
    Fetches relevant context from live data and indexed knowledge,
    then uses an LLM to synthesize an answer.

    Args:
        query_text: The natural language question from the user.

    Returns:
        A string containing the answer or an error message.
    """
    # Get OpenAI client (main.py:1438)
    openai_client = get_openai_client()
    if not openai_client:
        logger.error("RAG Query: OpenAI client is not available. Cannot process query.")
        return "RAG Error: OpenAI client not available. Please check server configuration and OpenAI API key."

    conn = None
    answer = (
        "An unexpected error occurred during the RAG query."  # Default error message
    )

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        live_context_results: List[Dict[str, Any]] = []
        live_task_results: List[Dict[str, Any]] = []
        vector_search_results: List[Dict[str, Any]] = (
            []
        )  # Store as dicts for easier access

        # --- 1. Fetch Live Context (Recently Updated) ---
        # PR F: rag_repo.get_last_indexed + fetch_recent_context own
        # the rag_meta read and the project_context time-windowed
        # fetch. The repo normalises the column rename from
        # ``last_updated`` -> ``updated_at`` (Phase 7b) so callers
        # don't have to remember which name to use.
        from ...repositories import rag_repo

        try:
            last_indexed_context_time = (
                rag_repo.get_last_indexed("context")
                or "1970-01-01T00:00:00Z"
            )
            live_context_results = rag_repo.fetch_recent_context(
                since=last_indexed_context_time, limit=5,
            )
            # SECURITY: never surface secret-keyed rows (config_*_token
            # etc.) to the LLM — the RAG answer is readable by any worker
            # via ask_project_rag, bypassing view_project_context's
            # redaction. Same policy as the tool boundary.
            live_context_results = [
                r for r in live_context_results
                if not _is_secret_key(r.get("context_key"))
                and not _value_has_embedded_secret(
                    r.get("value"), r.get("description")
                )
            ]
        except sqlite3.Error as e_live_ctx:
            logger.warning(
                f"RAG Query: Failed to fetch live project context: {e_live_ctx}"
            )
        except Exception as e_live_ctx_other:  # Catch any other unexpected error
            logger.warning(
                f"RAG Query: Unexpected error fetching live project context: {e_live_ctx_other}",
                exc_info=True,
            )

        # --- 2. Fetch Live Tasks (Keyword Search) ---
        # Original main.py: lines 1459 - 1477
        try:
            query_keywords = [
                f"%{word.strip().lower()}%"
                for word in query_text.split()
                if len(word.strip()) > 2
            ]
            if query_keywords:
                # Build LIKE clauses for title and description
                # Ensure each keyword is used for both title and description search
                conditions = []
                sql_params_tasks: List[str] = []
                for kw in query_keywords:
                    conditions.append("LOWER(title) LIKE ?")
                    sql_params_tasks.append(kw)
                    conditions.append("LOWER(description) LIKE ?")
                    sql_params_tasks.append(kw)

                if conditions:
                    # Validate that all conditions are safe (only LIKE patterns)
                    safe_conditions = []
                    for condition in conditions:
                        if condition not in [
                            "LOWER(title) LIKE ?",
                            "LOWER(description) LIKE ?",
                        ]:
                            logger.warning(
                                f"RAG Query: Skipping unsafe condition: {condition}"
                            )
                            continue
                        safe_conditions.append(condition)

                    if safe_conditions:
                        where_clause = " OR ".join(safe_conditions)
                        task_query_sql = f"""
                            SELECT task_id, title, status, description, updated_at
                            FROM tasks
                            WHERE {where_clause}
                            ORDER BY updated_at DESC
                            LIMIT 5
                        """
                        cursor.execute(task_query_sql, sql_params_tasks)
                    live_task_results = [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e_live_task:
            logger.warning(
                f"RAG Query: Failed to fetch live tasks based on query keywords: {e_live_task}"
            )
        except Exception as e_live_task_other:
            logger.warning(
                f"RAG Query: Unexpected error fetching live tasks: {e_live_task_other}",
                exc_info=True,
            )

        # --- 3. Perform Vector Search (Indexed Knowledge) ---
        # PR F: rag_repo.search_similar owns the vec0 ``MATCH`` +
        # ``k = ?`` ORDER BY distance dialect, the
        # rag_embeddings-existence guard, AND the metadata-JSON
        # hydration. The caller's job is to embed the query and pass
        # the vector in.
        if is_vss_loadable():
            try:
                response = openai_client.embeddings.create(
                    input=[query_text],
                    model=EMBEDDING_MODEL,
                    dimensions=EMBEDDING_DIMENSION,
                )
                query_embedding = response.data[0].embedding
                # k=13 is the legacy knn-results constant the previous
                # raw SQL used; preserved exactly so retrieval quality
                # is unchanged across the migration.
                vector_search_results = rag_repo.search_similar(
                    query_embedding=query_embedding,
                    limit=13,
                )
                # Retrieval-time defense against a stale index that
                # already embedded a secret context row.
                vector_search_results = _drop_secret_context_chunks(
                    vector_search_results
                )
            except (
                openai.APIError
            ) as e_openai_emb:  # Catch OpenAI errors during embedding
                logger.error(
                    f"RAG Query: OpenAI API error during query embedding: {e_openai_emb}"
                )
            except Exception as e_vec_other:
                logger.error(
                    f"RAG Query: Unexpected error during vector search part: {e_vec_other}",
                    exc_info=True,
                )
        else:
            logger.warning(
                "RAG Query: Vector search (sqlite-vec) is not available. Skipping vector search."
            )

        # --- 4. Combine Contexts for LLM ---
        # Original main.py: lines 1509 - 1548
        context_parts: List[str] = []
        current_token_count: int = 0  # Approximate token count

        # Add Live Context. Note: Phase 7b renamed the project_context
        # column ``last_updated`` -> ``updated_at``. The pre-PR-F code
        # here referenced the old name (a latent bug; the SELECT used
        # ``last_updated`` too, so the legacy path threw KeyError on
        # a post-Phase-7b DB and silently returned an empty list).
        # Through rag_repo.fetch_recent_context the field is now
        # ``updated_at`` end-to-end.
        if live_context_results:
            context_parts.append("--- Recently Updated Project Context (Live) ---")
            for item in live_context_results:
                entry_text = f"Key: {item['context_key']}\nValue: {item['value']}\nDescription: {item.get('description', 'N/A')}\n(Updated: {item['updated_at']})\n"
                entry_tokens = len(entry_text.split())  # Approximation
                if current_token_count + entry_tokens < MAX_CONTEXT_TOKENS:
                    context_parts.append(entry_text)
                    current_token_count += entry_tokens
                else:
                    break
            context_parts.append("---------------------------------------------")

        # Add Live Tasks
        if live_task_results:
            context_parts.append("--- Potentially Relevant Tasks (Live) ---")
            for task in live_task_results:
                entry_text = f"Task ID: {task['task_id']}\nTitle: {task['title']}\nStatus: {task['status']}\nDescription: {task.get('description', 'N/A')}\n(Updated: {task['updated_at']})\n"
                entry_tokens = len(entry_text.split())
                if current_token_count + entry_tokens < MAX_CONTEXT_TOKENS:
                    context_parts.append(entry_text)
                    current_token_count += entry_tokens
                else:
                    break
            context_parts.append("---------------------------------------")

        # Add Indexed Knowledge (Vector Search Results)
        if vector_search_results:
            context_parts.append(
                "--- Indexed Project Knowledge (Vector Search Results) ---"
            )
            for i, item in enumerate(vector_search_results):
                chunk_text = item["chunk_text"]
                source_type = item["source_type"]
                source_ref = item["source_ref"]
                metadata = item.get("metadata", {})
                distance = item.get("distance", "N/A")

                # Enhanced source info with metadata
                source_info = f"Source Type: {source_type}, Reference: {source_ref}"

                # Add code-specific metadata if available
                if metadata and source_type in ["code", "code_summary"]:
                    if metadata.get("language"):
                        source_info += f", Language: {metadata['language']}"
                    if metadata.get("section_type"):
                        source_info += f", Section: {metadata['section_type']}"
                    if metadata.get("entities"):
                        entity_names = [e.get("name", "") for e in metadata["entities"]]
                        if entity_names:
                            source_info += f", Contains: {', '.join(entity_names[:3])}"
                            if len(entity_names) > 3:
                                source_info += f" (+{len(entity_names)-3} more)"

                entry_text = f"Retrieved Chunk {i+1} (Similarity/Distance: {distance}):\n{source_info}\nContent:\n{chunk_text}\n"
                chunk_tokens = len(entry_text.split())
                if current_token_count + chunk_tokens < MAX_CONTEXT_TOKENS:
                    context_parts.append(entry_text)
                    current_token_count += chunk_tokens
                else:
                    context_parts.append(
                        "--- [Indexed knowledge truncated due to token limit] ---"
                    )
                    break
            context_parts.append(
                "-------------------------------------------------------"
            )

        if not context_parts:
            logger.info(
                f"RAG Query: No relevant information found for query: '{query_text}'"
            )
            answer = "No relevant information found in the project knowledge base or live data for your query."
        else:
            combined_context_str = "\n\n".join(context_parts)

            # --- 5. Call Chat Completion API ---
            # Original main.py: lines 1550 - 1562
            system_prompt_for_llm = """You are an AI assistant answering questions about a software project. 
Use the provided context, which may include recently updated live data (like project context keys or tasks) and information retrieved from an indexed knowledge base (like documentation or code summaries), to answer the user's query. 
Prioritize information from the 'Live' sections if available and relevant for time-sensitive data. 
Answer using *only* the information given in the context. If the context doesn't contain the answer, state that clearly.

Be VERBOSE and comprehensive in your responses. It's better to give too much context than too little. 
When answering, please also suggest additional context entries and queries that might be helpful for understanding this topic better.
For example, suggest related files to examine, related project context keys to check, or follow-up questions that could provide more insight.
Always err on the side of providing more detailed explanations and comprehensive information rather than brief responses."""

            user_message_for_llm = f"CONTEXT:\n{combined_context_str}\n\nQUERY:\n{query_text}\n\nBased *only* on the CONTEXT provided above, please answer the QUERY."

            # SECURITY (round-3): do NOT log the assembled context /
            # prompt excerpt — even after the secret filters above, a
            # low-signal credential could survive the heuristics and land
            # in DEBUG logs. Log only non-sensitive size metrics.
            logger.debug(
                "RAG Query: assembled context for LLM "
                "(approx tokens: %d, context chars: %d, prompt chars: %d)",
                current_token_count,
                len(combined_context_str),
                len(user_message_for_llm),
            )

            # Provider-agnostic chat call. completion_client() picks
            # Ollama vs OpenAI from env vars; both implement .chat().
            try:
                cc = completion_client()
            except CompletionConfigError as e_cfg:
                # SECURITY (round 9, SD-R9-1): do NOT reflect the config
                # exception text — it can carry env-var names / internal
                # paths and this string is returned verbatim to any
                # worker via ask_project_rag's Ok(message=...). Detail is
                # logged server-side; the caller gets a static category.
                logger.error(f"RAG Query: completion config error: {e_cfg}")
                return "RAG Error: completion provider is not configured"
            answer = await cc.chat(
                messages=[
                    {"role": "system", "content": system_prompt_for_llm},
                    {"role": "user", "content": user_message_for_llm},
                ],
                temperature=0.4,
            )

    # SECURITY (round 9, SD-R9-1): these arms return VERBATIM to any
    # worker via ask_project_rag's Ok(message=...)/data. Never embed the
    # exception (provider URLs / error bodies, SQL + table/column names,
    # filesystem paths). Log the detail server-side with exc_info; hand
    # the caller a static, category-preserving message.
    except openai.APIError as e_openai:  # main.py:1563
        logger.error(f"RAG Query: OpenAI API error: {e_openai}", exc_info=True)
        answer = "Error: RAG provider unavailable"
    except sqlite3.Error as e_sql:  # main.py:1566
        logger.error(f"RAG Query: Database error: {e_sql}", exc_info=True)
        answer = "Error: RAG query failed"
    except Exception as e_unexpected:  # main.py:1569
        logger.error(f"RAG Query: Unexpected error: {e_unexpected}", exc_info=True)
        answer = "An unexpected error occurred during the RAG query."
    finally:
        if conn:
            conn.close()

    return answer


async def query_rag_system_with_model(
    query_text: str, model_name: Optional[str] = None, max_tokens: int = None
) -> str:
    """
    Processes a query using the RAG system with a specific completion model.
    This is useful for task analysis with cheaper models while keeping
    main RAG queries on the premium model.

    Args:
        query_text: The natural language question from the user.
        model_name: Deprecated since v5.0.44 — kept for signature
            compatibility with existing call sites. The actual model
            is now selected by env vars via completion_client(); this
            argument is ignored.
        max_tokens: The maximum context tokens for this model

    Returns:
        A string containing the answer or an error message.
    """
    # Get OpenAI client
    openai_client = get_openai_client()
    if not openai_client:
        logger.error("RAG Query: OpenAI client is not available. Cannot process query.")
        return "RAG Error: OpenAI client not available. Please check server configuration and OpenAI API key."

    # Use provided max_tokens or default to the configured value
    context_limit = max_tokens if max_tokens else MAX_CONTEXT_TOKENS

    conn = None
    answer = "An unexpected error occurred during the RAG query."

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        live_context_results: List[Dict[str, Any]] = []
        live_task_results: List[Dict[str, Any]] = []
        vector_search_results: List[Dict[str, Any]] = []

        # Get live context (same as regular RAG). Phase 7b renamed
        # project_context.last_updated -> updated_at; this path still
        # referenced the old name and threw "no such column" on every
        # call, silently returning an empty live-context section. Fixed
        # to updated_at so the section works — and is secret-filtered.
        cursor.execute(
            "SELECT context_key, value, description, updated_at FROM project_context ORDER BY updated_at DESC"
        )
        live_context_results = [dict(row) for row in cursor.fetchall()]
        # SECURITY: drop secret-keyed rows before they reach the LLM
        # (same policy as query_rag_system / view_project_context).
        live_context_results = [
            r for r in live_context_results
            if not _is_secret_key(r.get("context_key"))
            and not _value_has_embedded_secret(
                r.get("value"), r.get("description")
            )
        ]

        # Get live tasks (same as regular RAG)
        cursor.execute(
            """
            SELECT task_id, title, description, status, created_by, assigned_to, 
                   priority, parent_task, depends_on_tasks, created_at, updated_at 
            FROM tasks 
            WHERE status IN ('pending', 'in_progress') 
            ORDER BY updated_at DESC
        """
        )
        live_task_results = [dict(row) for row in cursor.fetchall()]

        # Get vector search results if VSS is available.
        # PR F: rag_repo.search_similar owns the vec0 dialect; same
        # k=13 retrieval window as the main RAG path.
        if is_vss_loadable():
            try:
                from ...repositories import rag_repo

                query_embedding_response = openai_client.embeddings.create(
                    input=[query_text],
                    model=EMBEDDING_MODEL,
                    dimensions=EMBEDDING_DIMENSION,
                )
                query_embedding = query_embedding_response.data[0].embedding
                vector_search_results = rag_repo.search_similar(
                    query_embedding=query_embedding,
                    limit=13,
                )
                # Retrieval-time defense against a stale index that
                # already embedded a secret context row.
                vector_search_results = _drop_secret_context_chunks(
                    vector_search_results
                )
            except openai.APIError as e_openai_emb:
                logger.error(
                    f"RAG Query: OpenAI API error during query embedding: {e_openai_emb}"
                )
            except Exception as e_vec_other:
                logger.error(
                    f"RAG Query: Unexpected error during vector search: {e_vec_other}",
                    exc_info=True,
                )

        # Build context (same structure as regular RAG)
        context_parts = []
        current_token_count = 0

        # Include live context
        if live_context_results:
            context_parts.append("=== Live Project Context ===")
            for item in live_context_results:
                entry_text = f"Key: {item['context_key']}\nDescription: {item['description']}\nValue: {item['value']}\nLast Updated: {item['updated_at']}\n"
                chunk_tokens = len(entry_text.split())
                if current_token_count + chunk_tokens < context_limit:
                    context_parts.append(entry_text)
                    current_token_count += chunk_tokens
                else:
                    context_parts.append(
                        "--- [Live context truncated due to token limit] ---"
                    )
                    break

        # Include live tasks
        if live_task_results:
            context_parts.append("\n=== Live Task Information ===")
            for item in live_task_results:
                entry_text = f"Task ID: {item['task_id']}\nTitle: {item['title']}\nDescription: {item['description']}\nStatus: {item['status']}\n"
                entry_text += f"Priority: {item['priority']}\nAssigned To: {item['assigned_to']}\nCreated By: {item['created_by']}\n"
                entry_text += f"Parent Task: {item['parent_task']}\nDependencies: {item['depends_on_tasks']}\n"
                entry_text += (
                    f"Created: {item['created_at']}\nUpdated: {item['updated_at']}\n"
                )
                chunk_tokens = len(entry_text.split())
                if current_token_count + chunk_tokens < context_limit:
                    context_parts.append(entry_text)
                    current_token_count += chunk_tokens
                else:
                    context_parts.append(
                        "--- [Live tasks truncated due to token limit] ---"
                    )
                    break

        # Include vector search results
        if vector_search_results:
            context_parts.append("\n=== Retrieved from Indexed Knowledge ===")
            for i, item in enumerate(vector_search_results):
                chunk_text = item["chunk_text"]
                source_type = item["source_type"]
                source_ref = item["source_ref"]
                metadata = item.get("metadata", {})
                distance = item.get("distance", "N/A")

                # Enhanced source info with metadata (matching working implementation)
                source_info = f"Source Type: {source_type}, Reference: {source_ref}"

                # Add code-specific metadata if available
                if metadata and source_type in ["code", "code_summary"]:
                    if metadata.get("language"):
                        source_info += f", Language: {metadata['language']}"
                    if metadata.get("section_type"):
                        source_info += f", Section: {metadata['section_type']}"
                    if metadata.get("entities"):
                        entity_names = [e.get("name", "") for e in metadata["entities"]]
                        if entity_names:
                            source_info += f", Contains: {', '.join(entity_names[:3])}"
                            if len(entity_names) > 3:
                                source_info += f" (+{len(entity_names)-3} more)"

                entry_text = f"Retrieved Chunk {i+1} (Similarity/Distance: {distance}):\n{source_info}\nContent:\n{chunk_text}\n"
                chunk_tokens = len(entry_text.split())
                if current_token_count + chunk_tokens < context_limit:
                    context_parts.append(entry_text)
                    current_token_count += chunk_tokens
                else:
                    context_parts.append(
                        "--- [Indexed knowledge truncated due to token limit] ---"
                    )
                    break

        if not context_parts:
            logger.info(
                f"RAG Query: No relevant information found for query: '{query_text}'"
            )
            answer = "No relevant information found in the project knowledge base or live data for your query."
        else:
            combined_context_str = "\n\n".join(context_parts)

            # Call Chat Completion API with specified model
            system_prompt_for_llm = """You are an AI assistant specializing in task hierarchy analysis and project structure optimization. 
You must CRITICALLY THINK about task placement, dependencies, and hierarchical relationships.
Use the provided context to make intelligent recommendations about task organization.
Be strict about the single root task rule and logical task relationships.

Be VERBOSE and comprehensive in your analysis. It's better to give too much context than too little.
When making recommendations, suggest additional context entries and queries that might be helpful for understanding task relationships better.
Consider suggesting related files to examine, project context keys to check, or follow-up questions for deeper task analysis.
Provide detailed explanations for your reasoning and comprehensive information rather than brief responses.
Answer in the exact JSON format requested, but include thorough explanations in your reasoning sections."""

            user_message_for_llm = f"CONTEXT:\n{combined_context_str}\n\nQUERY:\n{query_text}\n\nBased on the CONTEXT provided above, please answer the QUERY."

            # Provider-agnostic chat call (v5.0.44). The
            # ``model_name`` arg is now informational only — env vars
            # select the provider & model. Log it so operators can
            # see what context_limit was requested.
            try:
                cc = completion_client()
            except CompletionConfigError as e_cfg:
                # SECURITY (round 9, SD-R9-1): static string, no e_cfg
                # (consistency with query_rag_system's arm above).
                logger.error(
                    f"RAG Query (task analysis): completion config error: {e_cfg}"
                )
                return "RAG Error: completion provider is not configured"
            logger.info(
                f"Task Analysis Query: using {cc.provider}/{cc.model} "
                f"(context_limit={context_limit})"
            )
            answer = await cc.chat(
                messages=[
                    {"role": "system", "content": system_prompt_for_llm},
                    {"role": "user", "content": user_message_for_llm},
                ],
                temperature=0.4,
            )

    except Exception as e:
        # SECURITY (round 9, SD-R9-1): static string, no str(e).
        logger.error(f"RAG Query (task analysis): Error: {e}", exc_info=True)
        answer = "An unexpected error occurred during the RAG task-analysis query."
    finally:
        if conn:
            conn.close()

    return answer
