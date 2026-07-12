# Agent-MCP/mcp_template/mcp_server_src/features/rag/query.py
import sqlite3  # For type hinting and error handling
from typing import Any, Callable, Dict, List, Optional

# Imports from our project
from ...core.config import (
    logger,
    MAX_CONTEXT_TOKENS,  # From main.py:182
)
from ...db.connection import get_db_connection, is_vss_loadable
from ...external.embedding_service import embedding_client
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


# SECRET REDACTION OWNERSHIP: the retrieval SEAM
# (``rag_repo.search_similar`` + ``rag_repo.fetch_recent_context``) owns
# secret redaction — the seam that returns the data drops the secrets.
# Both query_rag_system and query_rag_system_with_model read live
# context through ``rag_repo.fetch_recent_context`` (arch-r5 #4
# collapsed the second, hand-rolled ``SELECT ... FROM project_context``
# + inline filter that query_rag_system_with_model used to carry — see
# git history for the pre-collapse duplicate). One redaction
# enforcement point, one seam, one thing to keep correct.
#
# ``_drop_secret_chunks`` below is explicit defense-in-depth,
# NOT the primary guard: it protects callers that inject/mock the repo
# and bypass the real ``search_similar`` seam.
def _value_has_embedded_secret(*texts: Optional[str]) -> bool:
    """Lazy wrapper over the canonical embedded-secret VALUE scanner.

    Imported lazily (not at module top) to sidestep the tools/rag import
    cycle. Reused (not duplicated) from the index path so this filter
    applies the SAME skip the ingest choke-point does. Variadic to mirror
    the underlying scanner: a single call can screen every text field of a
    row (e.g. a task ``title`` AND ``description``) in one pass.
    """
    from .indexing import _value_has_embedded_secret as _scan

    return _scan(*texts)


def _drop_secret_chunks(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop retrieved vector chunks that carry a credential — for ANY
    source type. Defense-in-depth mirroring ``rag_repo.search_similar``'s
    own seam-level drop, for the case where a caller injects/mocks the
    repo and bypasses the real seam. Two drop rules, both retained:

    * secret-KEYED ``project_context`` row (``source_ref`` is the
      context_key for ``source_type == "context"``).
    * secret in the chunk TEXT itself — a credential in a task
      description/title, code file, or markdown doc (R2-F3); covers every
      source type, not just ``context``.
    """
    return [
        r
        for r in results
        if not (
            (
                r.get("source_type") == "context"
                and _is_secret_key(r.get("source_ref"))
            )
            or _value_has_embedded_secret(r.get("chunk_text"))
        )
    ]


def _drop_secret_tasks(
    tasks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop live-task rows whose ``title`` or ``description`` embeds a
    credential (R2-F3b).

    ``query_rag_system`` stage 2 (and its ``_with_model`` sibling) fetch
    tasks with a raw ``SELECT ... FROM tasks`` and render them straight
    into the LLM context. That fetch is NOT the ``rag_chunks`` ingest
    choke-point #463 hardened, nor the ``search_similar`` retrieval seam
    ``_drop_secret_chunks`` guards — a secret pasted into a task
    description reached ``ask_project_rag`` verbatim (live RE_VERIFY of
    ``ghp_R2ReVerify1111closedCHECKzzzz9999``). This mirrors, for the
    live-task source, exactly how stage-1 live-context
    (``fetch_recent_context``) and stage-3 chunks (``_drop_secret_chunks``)
    are already filtered: DROP the offending row, uniform with the other
    seams. Reuses the same ``_value_has_embedded_secret`` scanner the
    ingest choke-point uses — one detector, one policy.
    """
    return [
        t
        for t in tasks
        if not _value_has_embedded_secret(t.get("title"), t.get("description"))
    ]


def _scrub_secret_parts(parts: List[str]) -> List[str]:
    """SECURITY (R2-F3b) — the final assembly-seam scrub.

    This is the single choke-point every RAG context source flows through
    before it reaches the LLM (``_assemble_and_answer`` joins ``parts`` into
    the prompt). Stages 1-3 each have their own upstream filter, but the
    original R2-F3 miss proved a per-stage sweep can be left incomplete:
    stage 2 (live tasks) shipped unscanned and re-leaked a token. Scanning
    every assembled part HERE makes secret redaction by-construction — it
    covers all four current stages AND any future 5th context source added
    upstream, so this class can never silently reopen. DROP the offending
    part (uniform with the other seams' drop semantics); each task/chunk is
    its own part, so dropping one leaks-bearing entry keeps the rest.
    """
    return [p for p in parts if not _value_has_embedded_secret(p)]


# ── Shared assembly helpers ───────────────────────────────────────────
#
# query_rag_system and query_rag_system_with_model both run the same
# 4-stage pipeline (fetch live context → fetch live tasks → vector-search
# chunks → assemble under a token budget → chat completion). The three
# helpers below centralise the pieces that were byte-for-byte duplicated
# between the two functions; the genuinely different pieces (retrieval
# preambles, task filters, section headers, system prompts) stay in each
# public function.


def _append_within_budget(
    parts: List[str], entry_text: str, count: int, limit: int
) -> Optional[int]:
    """Append ``entry_text`` to ``parts`` if it fits the token budget.

    This is the 6x-duplicated accumulation-loop body (three sections x
    two query functions). Preserves the exact pre-existing boundary
    semantics: an entry that would bring the running count to exactly
    ``limit`` is rejected (strict ``<``, not ``<=``) — see
    ``tests/test_arch_r5_4_rag_query_dedup.py`` for the boundary pin.

    Returns the new running token count on success, or ``None`` if the
    entry did not fit (the caller should stop adding further entries to
    this section and may append its own truncation marker).
    """
    entry_tokens = len(entry_text.split())  # Approximation
    if count + entry_tokens < limit:
        parts.append(entry_text)
        return count + entry_tokens
    return None


def _render_chunk(i: int, item: Dict[str, Any]) -> str:
    """Render one retrieved vector-search chunk into its RAG context
    text block. Byte-for-byte identical between the two query
    functions pre-refactor (metadata/source-info builder + entry
    format)."""
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

    return (
        f"Retrieved Chunk {i+1} (Similarity/Distance: {distance}):\n"
        f"{source_info}\nContent:\n{chunk_text}\n"
    )


async def _assemble_and_answer(
    context_parts: List[str],
    current_token_count: int,
    query_text: str,
    *,
    system_prompt: str,
    answer_instruction: str,
    log_label: str,
    on_client_ready: Optional[Callable[[Any], None]] = None,
) -> str:
    """Stage 4 (final join) + stage 5 (chat completion) — shared by both
    query functions.

    ``answer_instruction`` and ``log_label`` carry the two functions'
    genuine wording differences (the "*only*" emphasis in
    query_rag_system's user message; the "(task analysis)" log-message
    suffix in query_rag_system_with_model) so both functions' external
    behaviour — including log text — is unchanged by the extraction.
    ``on_client_ready`` lets query_rag_system_with_model log the
    provider/model it resolved, which query_rag_system does not do.
    """
    # SECURITY (R2-F3b): final assembly-seam scrub. See _scrub_secret_parts
    # — this one seam covers every current AND future context source, so a
    # secret can never reach the LLM even if an upstream stage's own filter
    # is missing or a new source is added without one.
    context_parts = _scrub_secret_parts(context_parts)
    if not context_parts:
        logger.info(
            f"{log_label}: No relevant information found for query: '{query_text}'"
        )
        return (
            "No relevant information found in the project knowledge base "
            "or live data for your query."
        )

    combined_context_str = "\n\n".join(context_parts)
    user_message_for_llm = (
        f"CONTEXT:\n{combined_context_str}\n\nQUERY:\n{query_text}\n\n"
        f"{answer_instruction}"
    )

    # SECURITY (round-3): do NOT log the assembled context / prompt
    # excerpt — even after the secret filters above, a low-signal
    # credential could survive the heuristics and land in DEBUG logs.
    # Log only non-sensitive size metrics.
    logger.debug(
        f"{log_label}: assembled context for LLM "
        "(approx tokens: %d, context chars: %d, prompt chars: %d)",
        current_token_count,
        len(combined_context_str),
        len(user_message_for_llm),
    )

    # Provider-agnostic chat call. completion_client() picks Ollama vs
    # OpenAI from env vars; both implement .chat().
    try:
        cc = completion_client()
    except CompletionConfigError as e_cfg:
        # SECURITY (round 9, SD-R9-1): do NOT reflect the config
        # exception text — it can carry env-var names / internal paths
        # and this string is returned verbatim to any worker via
        # ask_project_rag's Ok(message=...). Detail is logged
        # server-side; the caller gets a static category.
        logger.error(f"{log_label}: completion config error: {e_cfg}")
        return "RAG Error: completion provider is not configured"

    if on_client_ready is not None:
        on_client_ready(cc)

    return await cc.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message_for_llm},
        ],
        temperature=0.4,
    )


# Original location: main.py lines 1432 - 1566 (ask_project_rag_tool function body)

_SYSTEM_PROMPT_GENERAL = """You are an AI assistant answering questions about a software project. 
Use the provided context, which may include recently updated live data (like project context keys or tasks) and information retrieved from an indexed knowledge base (like documentation or code summaries), to answer the user's query. 
Prioritize information from the 'Live' sections if available and relevant for time-sensitive data. 
Answer using *only* the information given in the context. If the context doesn't contain the answer, state that clearly.

Be VERBOSE and comprehensive in your responses. It's better to give too much context than too little. 
When answering, please also suggest additional context entries and queries that might be helpful for understanding this topic better.
For example, suggest related files to examine, related project context keys to check, or follow-up questions that could provide more insight.
Always err on the side of providing more detailed explanations and comprehensive information rather than brief responses."""


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
            # SECURITY: fetch_recent_context is the retrieval seam and
            # owns secret redaction — secret-keyed rows (config_*_token
            # etc.) and embedded-credential values are already dropped
            # before they return here.
            live_context_results = rag_repo.fetch_recent_context(
                since=last_indexed_context_time, limit=5,
            )
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
                    # SECURITY (R2-F3b): this raw task fetch is NOT the
                    # rag_chunks ingest choke-point nor the search_similar
                    # retrieval seam, so it must apply the SAME embedded-
                    # secret drop the other stages do — a token in a task
                    # title/description reached the LLM verbatim otherwise.
                    live_task_results = _drop_secret_tasks(
                        [dict(row) for row in cursor.fetchall()]
                    )
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
                query_embedding = embedding_client().embed([query_text])[0]
                # k=13 is the legacy knn-results constant the previous
                # raw SQL used; preserved exactly so retrieval quality
                # is unchanged across the migration.
                # search_similar (the seam) already drops secret context
                # chunks; the wrap here is defense-in-depth for an
                # injected/mocked repo that bypasses the real seam.
                vector_search_results = _drop_secret_chunks(
                    rag_repo.search_similar(
                        query_embedding=query_embedding,
                        limit=13,
                    )
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
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, MAX_CONTEXT_TOKENS
                )
                if new_count is None:
                    break
                current_token_count = new_count
            context_parts.append("---------------------------------------------")

        # Add Live Tasks
        if live_task_results:
            context_parts.append("--- Potentially Relevant Tasks (Live) ---")
            for task in live_task_results:
                entry_text = f"Task ID: {task['task_id']}\nTitle: {task['title']}\nStatus: {task['status']}\nDescription: {task.get('description', 'N/A')}\n(Updated: {task['updated_at']})\n"
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, MAX_CONTEXT_TOKENS
                )
                if new_count is None:
                    break
                current_token_count = new_count
            context_parts.append("---------------------------------------")

        # Add Indexed Knowledge (Vector Search Results)
        if vector_search_results:
            context_parts.append(
                "--- Indexed Project Knowledge (Vector Search Results) ---"
            )
            for i, item in enumerate(vector_search_results):
                entry_text = _render_chunk(i, item)
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, MAX_CONTEXT_TOKENS
                )
                if new_count is None:
                    context_parts.append(
                        "--- [Indexed knowledge truncated due to token limit] ---"
                    )
                    break
                current_token_count = new_count
            context_parts.append(
                "-------------------------------------------------------"
            )

        # --- 5. Call Chat Completion API ---
        # Original main.py: lines 1509 - 1562
        answer = await _assemble_and_answer(
            context_parts,
            current_token_count,
            query_text,
            system_prompt=_SYSTEM_PROMPT_GENERAL,
            answer_instruction=(
                "Based *only* on the CONTEXT provided above, please "
                "answer the QUERY."
            ),
            log_label="RAG Query",
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


_SYSTEM_PROMPT_TASK_ANALYSIS = """You are an AI assistant specializing in task hierarchy analysis and project structure optimization. 
You must CRITICALLY THINK about task placement, dependencies, and hierarchical relationships.
Use the provided context to make intelligent recommendations about task organization.
Be strict about the single root task rule and logical task relationships.

Be VERBOSE and comprehensive in your analysis. It's better to give too much context than too little.
When making recommendations, suggest additional context entries and queries that might be helpful for understanding task relationships better.
Consider suggesting related files to examine, project context keys to check, or follow-up questions for deeper task analysis.
Provide detailed explanations for your reasoning and comprehensive information rather than brief responses.
Answer in the exact JSON format requested, but include thorough explanations in your reasoning sections."""


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
    # Use provided max_tokens or default to the configured value
    context_limit = max_tokens if max_tokens else MAX_CONTEXT_TOKENS

    conn = None
    answer = "An unexpected error occurred during the RAG task-analysis query."

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        live_context_results: List[Dict[str, Any]] = []
        live_task_results: List[Dict[str, Any]] = []
        vector_search_results: List[Dict[str, Any]] = []

        # Get live context (same as regular RAG), through the SAME
        # retrieval seam query_rag_system uses. arch-r5 #4: this used
        # to be a hand-rolled ``SELECT ... FROM project_context`` with
        # its own inline ``_is_secret_key`` / ``_value_has_embedded_
        # secret`` filter — a second, independently-maintained
        # redaction enforcement point. Routing through
        # rag_repo.fetch_recent_context collapses that to the ONE seam
        # query_rag_system already uses. ``since`` = epoch and
        # ``limit=None`` reproduce this function's original unbounded,
        # un-windowed read (it never had a "recently changed" time
        # filter, unlike query_rag_system): every row (updated_at is
        # NOT NULL, so every real timestamp compares greater than the
        # epoch string) with no SQL row cap, matching the pre-refactor
        # behaviour exactly. Phase 7b renamed
        # project_context.last_updated -> updated_at; this path still
        # referenced the old name and threw "no such column" on every
        # call, silently returning an empty live-context section. Fixed
        # to updated_at so the section works.
        from ...repositories import rag_repo

        live_context_results = rag_repo.fetch_recent_context(
            since="1970-01-01T00:00:00Z", limit=None,
        )

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
        # SECURITY (R2-F3b): same raw-task-fetch drop as query_rag_system's
        # stage 2 — a secret in a task title/description must not reach the
        # LLM through the task-analysis path either.
        live_task_results = _drop_secret_tasks(
            [dict(row) for row in cursor.fetchall()]
        )

        # Get vector search results if VSS is available.
        # PR F: rag_repo.search_similar owns the vec0 dialect; same
        # k=13 retrieval window as the main RAG path.
        if is_vss_loadable():
            try:
                query_embedding = embedding_client().embed([query_text])[0]
                # search_similar (the seam) already drops secret context
                # chunks; the wrap here is defense-in-depth for an
                # injected/mocked repo that bypasses the real seam.
                vector_search_results = _drop_secret_chunks(
                    rag_repo.search_similar(
                        query_embedding=query_embedding,
                        limit=13,
                    )
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
        context_parts: List[str] = []
        current_token_count = 0

        # Include live context
        if live_context_results:
            context_parts.append("=== Live Project Context ===")
            for item in live_context_results:
                entry_text = f"Key: {item['context_key']}\nDescription: {item['description']}\nValue: {item['value']}\nLast Updated: {item['updated_at']}\n"
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, context_limit
                )
                if new_count is None:
                    context_parts.append(
                        "--- [Live context truncated due to token limit] ---"
                    )
                    break
                current_token_count = new_count

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
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, context_limit
                )
                if new_count is None:
                    context_parts.append(
                        "--- [Live tasks truncated due to token limit] ---"
                    )
                    break
                current_token_count = new_count

        # Include vector search results
        if vector_search_results:
            context_parts.append("\n=== Retrieved from Indexed Knowledge ===")
            for i, item in enumerate(vector_search_results):
                entry_text = _render_chunk(i, item)
                new_count = _append_within_budget(
                    context_parts, entry_text, current_token_count, context_limit
                )
                if new_count is None:
                    context_parts.append(
                        "--- [Indexed knowledge truncated due to token limit] ---"
                    )
                    break
                current_token_count = new_count

        # Call Chat Completion API with specified model. The
        # ``model_name`` arg is now informational only — env vars
        # select the provider & model. Log it so operators can see
        # what context_limit was requested (via on_client_ready).
        def _log_provider(cc: Any) -> None:
            logger.info(
                f"Task Analysis Query: using {cc.provider}/{cc.model} "
                f"(context_limit={context_limit})"
            )

        answer = await _assemble_and_answer(
            context_parts,
            current_token_count,
            query_text,
            system_prompt=_SYSTEM_PROMPT_TASK_ANALYSIS,
            answer_instruction=(
                "Based on the CONTEXT provided above, please answer the QUERY."
            ),
            log_label="RAG Query (task analysis)",
            on_client_ready=_log_provider,
        )

    except Exception as e:
        # SECURITY (round 9, SD-R9-1): static string, no str(e).
        logger.error(f"RAG Query (task analysis): Error: {e}", exc_info=True)
        answer = "An unexpected error occurred during the RAG task-analysis query."
    finally:
        if conn:
            conn.close()

    return answer
