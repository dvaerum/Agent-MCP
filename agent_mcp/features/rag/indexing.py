# Agent-MCP/mcp_template/mcp_server_src/features/rag/indexing.py
import anyio
import time
import datetime
import json
import hashlib
import glob
import os
import sqlite3
from pathlib import Path
from typing import List, Dict, Tuple, Any, Optional, NoReturn

# Attempt to import the OpenAI library
try:
    import openai
except ImportError:
    openai = None

# Imports from our own project modules
from ...core.config import (
    logger,
    get_project_dir,
    embedding_settings,  # Resolve (model, dimension, advanced) at call time,
    # not at THIS module's import time — see EmbeddingSettings docstring.
)
from ...core import globals as g  # For server_running flag
from ...db.connection import get_db_connection, is_vss_loadable

# Provider-agnostic embedding seam: owns (model, dimension, base_url,
# api_key) and picks OpenAI-vs-Ollama from the same env vars the
# completion seam uses, so every "turn text into a vector" resolves one
# endpoint. Mirrors external.completion_service.completion_client().
from ...external.embedding_service import embedding_client

# Import chunking functions from this RAG feature package
from .chunking import simple_chunker, markdown_aware_chunker
from .code_chunking import (
    chunk_code_aware,
    detect_language_family,
    extract_code_entities,
    create_file_summary,
    CODE_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
)

# Original location: main.py lines 512 - 826 (run_rag_indexing_periodically function and its logic)

# Define patterns to ignore for file scanning, as in the original
# Original main.py: 548-552
IGNORE_DIRS_FOR_INDEXING = [
    "node_modules",
    "__pycache__",
    "venv",
    "env",
    ".venv",
    ".env",
    "dist",
    "build",
    "site-packages",
    ".git",
    ".idea",
    ".vscode",
    "bin",
    "obj",
    "target",
    ".pytest_cache",
    ".ipynb_checkpoints",
    ".agent",  # Also ignore the .agent directory itself
]

# ADR-0017 (Wave 12 PR B): content-based secret detection is GONE. The
# embedded-secret value scanner (``_value_has_embedded_secret`` +
# ``_EMBEDDED_SECRET_PATTERNS`` + the high-entropy runs) and the one-time
# secret-purge machinery (``_CTX_SECRET_PURGE_META_KEY`` /
# ``_ALLSOURCE_SECRET_PURGE_META_KEY`` + their purge helpers) were an
# unreliable denylist (false positives evicted legitimate memory notes /
# code / markdown from the index; false negatives leaked novel formats).
# memory, tasks, code, and markdown are shared project content — indexed
# AS-IS. Real secrets belong in sops refs or the operator-only, non-RAG
# project_settings store; protection is by authorization (RAG is
# per-project and ownership-scoped), not by guessing content.


# Increased concurrency for Tier 3 pricing (5000 RPM)
# Original main.py: 654
MAX_CONCURRENT_EMBEDDING_REQUESTS = 25
# Use smaller batch size for more parallelism
# Original main.py: 660
PARALLEL_EMBEDDING_BATCH_SIZE = 50


async def _get_embeddings_batch(
    batch_chunks: List[str],
    batch_index_start: int,
    results_list: List[Optional[List[float]]],
) -> bool:
    """
    Processes a single batch of embeddings asynchronously through the
    embedding seam (:func:`embedding_client`). A fresh client per batch
    preserves the original "separate async client per batch for true
    concurrency" behaviour.
    This is a helper for run_rag_indexing_periodically.
    Based on original main.py: lines 656-675.
    """
    # Need to import openai here if not at module level for type hints,
    # or ensure it's available. It's imported at module level with try-except.
    if openai is None:  # Check if openai library was imported successfully
        logger.error("OpenAI library not available for embedding batch.")
        for i in range(len(batch_chunks)):
            if batch_index_start + i < len(results_list):
                results_list[batch_index_start + i] = None  # Mark as failed
        return False

    try:
        # Validate batch_chunks before sending to API
        validated_chunks = []
        for chunk in batch_chunks:
            if isinstance(chunk, str) and chunk.strip():
                validated_chunks.append(chunk)
            else:
                logger.warning(
                    f"Invalid chunk in batch: {type(chunk)} - {repr(chunk)[:50]}"
                )
                validated_chunks.append(
                    " "
                )  # Use single space as fallback to maintain batch size

        # A fresh embedding client per batch preserves the original
        # "separate async client for true concurrency" behaviour; the
        # seam owns model/dimension/base_url/api_key so the endpoint is
        # resolved the same way as every other embedding call site.
        vectors = await embedding_client().aembed(validated_chunks)
        # Store results directly in the provided results list
        for j, vector in enumerate(vectors):
            pos = batch_index_start + j
            if pos < len(results_list):
                results_list[pos] = vector
        # logger.info(f"Completed embedding batch starting at index {batch_index_start}") # Original: main.py:672
        return True
    except Exception as e:
        logger.error(
            f"OpenAI embedding API error in batch starting at {batch_index_start}: {e}"
        )
        # Mark all embeddings in this batch as failed (None)
        for i in range(len(batch_chunks)):
            if batch_index_start + i < len(results_list):
                results_list[batch_index_start + i] = None
        return False


def _delete_stale_chunks_and_commit(
    cursor: sqlite3.Cursor,
    conn: sqlite3.Connection,
    sources_to_process_for_embedding: List[Tuple[str, str, str, str]],
) -> int:
    """Purge every stale chunk for ``sources_to_process_for_embedding``
    and commit, UNCONDITIONALLY, before returning.

    Returns the total ``rag_chunks`` row count deleted (for logging
    only — callers must not use it to decide whether to commit; see
    R10-F2 below).

    R10-F2: the caller (:func:`run_rag_indexing_periodically`) shares
    one long-lived ``conn`` across an entire indexing cycle, including
    the embedding phase that follows this delete pass — a sequence of
    ``await`` points on a (possibly slow, network-bound) embedding
    endpoint that can run for seconds to minutes under load. This used
    to gate the interim commit on ``delete_count > 0``:

        if delete_count > 0:
            conn.commit()

    but a DELETE statement opens sqlite's write transaction (and holds
    its WAL write lock) the moment it EXECUTES, regardless of how many
    rows it actually matched. A cycle indexing only brand-new sources
    (nothing to delete yet, so every ``rag_repo.delete_chunks_for``
    call returns 0) satisfied that DELETE-executed-but-nothing-matched
    case on EVERY source, so ``delete_count`` stayed 0, the commit was
    skipped, and the connection sat in an open, uncommitted transaction
    for the entire embedding-await phase that followed — starving
    every OTHER writer in the process (e.g. a concurrent
    ``delete_project_context`` / ``create_task`` request via
    ``unit_of_work()``) well past their 5s ``busy_timeout`` with
    ``sqlite3.OperationalError: database is locked``, for as long as
    the embedding phase ran. Committing unconditionally here — whether
    or not any row actually matched — releases the lock immediately,
    before the awaits begin.
    """
    from ...repositories import rag_repo

    delete_count = 0
    for source_type, source_ref, _content, _hash in sources_to_process_for_embedding:
        n_deleted = rag_repo.delete_chunks_for(
            source_type, source_ref, connection=cursor,
        )
        delete_count += n_deleted
    if delete_count > 0:
        logger.info(
            f"Deleted {delete_count} old chunks and their embeddings."
        )
    # Unconditional: see the docstring above — a 0-row DELETE still
    # opened the transaction and still holds the write lock.
    conn.commit()
    return delete_count


def _watermark_after_failures(
    old_watermark: Any,
    uncapped_max: Any,
    source_type: str,
    source_mod_time: Dict[Tuple[str, str], Any],
    fully_embedded_sources: set,
    sources_with_failed_chunks: set,
) -> Any:
    """Cap an incremental RAG watermark so it never advances past a row
    that failed to embed this cycle (BL-R31-1).

    The periodic indexer selects rows with ``updated_at > watermark``.
    If the watermark advanced to the max ``updated_at`` of *scanned*
    rows while some of them failed to embed, those rows are never
    re-selected and ``ask_project_rag`` serves them stale forever. This
    holds the watermark strictly below the earliest failed row's
    mod-time so it is re-scanned next cycle, while still advancing over
    the rows that embedded cleanly below it.

    ``source_type`` filters the source keys (both sets key on the row's
    scan type — e.g. ``"context"`` / ``"task"``). ``old_watermark`` is
    the value already stored in ``rag_meta`` (used as the floor so the
    watermark never regresses); ``uncapped_max`` is the value the caller
    would have written with no failures. All compared values share a
    type per source_type (ISO strings for context/task, float mtimes for
    markdown/code), so the ``min``/``max`` are well-defined.

    Trade-off: a permanently-failing row pins the watermark at its
    predecessor, so newer clean rows above it are re-scanned every cycle
    — cheap, since their hash matches and they are not re-embedded.
    Content still reaches the index; only the watermark is held. This is
    the intended bounded-retry behaviour.
    """
    failed_times = [
        source_mod_time[k]
        for k in sources_with_failed_chunks
        if k[0] == source_type and k in source_mod_time
    ]
    if not failed_times:
        return uncapped_max
    earliest_failed = min(failed_times)
    candidates = [old_watermark]
    for k in fully_embedded_sources:
        if k[0] == source_type and k in source_mod_time:
            mod_time = source_mod_time[k]
            if mod_time < earliest_failed:
                candidates.append(mod_time)
    return max(candidates)


async def run_rag_indexing_periodically(
    interval_seconds: int = 300, *, task_status=anyio.TASK_STATUS_IGNORED
) -> NoReturn:
    """
    Periodically scans sources (Markdown files, project context) and updates
    the RAG index in the database.
    Original main.py: lines 512 - 826.
    """
    logger.info("Background RAG indexer process starting...")
    # Signal that the task has started successfully for the TaskGroup
    task_status.started()

    # Defer first cycle until lifespan startup finishes. RAG opens its
    # own DB connections via `get_db_connection()` and resolves paths
    # via `get_project_dir()`; both require MCP_PROJECT_DIR which is
    # set inside `app.server_lifecycle.application_startup`. The
    # previous 10s `anyio.sleep` was an implicit margin for the same
    # ordering; the explicit `await` removes the timing assumption.
    await g.startup_complete_event.wait()

    # No OPENAI_API_KEY liveness gate here (arch-r4 #2): the indexer
    # used to hard-abort the whole background loop when
    # OPENAI_API_KEY was empty, on the theory that RAG needs an
    # OpenAI key. That's wrong for the documented Ollama-default
    # deployment (see completion_service.py's module docstring) —
    # `embedding_client()` (below) resolves OpenAI-vs-Ollama from env
    # vars and works fine with no OpenAI key at all. The old guard
    # meant an Ollama-only deploy could QUERY the vec index (query.py
    # has no such guard) but the WRITER bailed on cycle one, leaving
    # the index permanently empty. "Can I embed?" now has exactly one
    # answer — resolved by embedding_client() — for both read and
    # write, matching how features/rag/query.py already resolves it.

    # Check if the openai package itself was importable (a hard dep of
    # embedding_client() regardless of provider).
    if openai is None:
        logger.error("OpenAI Python library not loaded. RAG indexer cannot run.")
        return

    while g.server_running:  # Uses global flag (main.py:521)
        cycle_start_time = time.time()

        # Resolved ONCE per cycle (not at module-import time) so a
        # runtime ``--advanced`` reconfigure is honoured immediately —
        # see EmbeddingSettings' docstring for the import-order bug this
        # closes.
        _emb_settings = embedding_settings()

        # Log what content will be indexed based on mode
        if _emb_settings.dimension == 3072:
            logger.info(
                "Starting RAG index update cycle (advanced mode: markdown, code, context, tasks)..."
            )
        else:
            logger.info(
                "Starting RAG index update cycle (simple mode: markdown, context only)..."
            )

        conn = None  # Initialize conn here for broader scope in try-finally

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Check if VSS is usable (vec0 table exists as a proxy)
            # Original main.py:526-531
            if (
                not is_vss_loadable()
            ):  # This checks the global flag set by initial check
                logger.warning(
                    "Vector Search (sqlite-vec) is not loadable. Skipping RAG indexing cycle."
                )
                await anyio.sleep(interval_seconds * 2)  # Sleep longer if VSS fails
                continue  # Skip to next iteration of the while loop

            # Check for rag_embeddings table specifically. PR F
            # exposes the existence-check via
            # ``agent_mcp.repositories.rag_repository._embeddings_table_exists``
            # (arch-deepening R3 #2b deleted the ``db.actions.rag_db``
            # re-export shim; import the repository directly) so the
            # gate check uses the same source-of-truth predicate.
            from ...repositories.rag_repository import _embeddings_table_exists
            if not _embeddings_table_exists(cursor):
                logger.warning(
                    "Vector table 'rag_embeddings' not found. Skipping RAG indexing cycle. Ensure DB schema is initialized."
                )
                await anyio.sleep(interval_seconds * 2)
                continue

            # Get last indexed timestamps and stored hashes.
            # PR F: rag_repo owns the rag_meta read surface now —
            # ``get_all_meta`` returns the whole table in one shot
            # so the partition into ``last_indexed_*`` /  ``hash_*``
            # keys stays exactly as before. We keep the partition
            # logic here because the indexer's hash-comparison loop
            # depends on the ``hash_<source_type>_<source_ref>`` key
            # shape (see the per-source filter below).
            from ...repositories import rag_repo

            rag_meta_data = rag_repo.get_all_meta()
            last_indexed_timestamps = {
                k: v for k, v in rag_meta_data.items() if k.startswith("last_indexed_")
            }
            stored_hashes = {
                k: v for k, v in rag_meta_data.items() if k.startswith("hash_")
            }

            # ADR-0017 (Wave 12 PR B): the one-time secret-purge blocks
            # (context + all-source) that used to evict "secret-bearing"
            # chunks from the index are removed — content-based secret
            # detection is gone. All content is indexed AS-IS.

            current_project_dir = get_project_dir()  # From config (main.py:537)
            sources_to_check: List[Tuple[str, str, str, Any, str]] = (
                []
            )  # type, ref, content, mod_time/iso, hash

            # 1. Scan Markdown Files and Code Files
            last_md_time_str = last_indexed_timestamps.get(
                "last_indexed_markdown", "1970-01-01T00:00:00Z"
            )
            last_code_time_str = last_indexed_timestamps.get(
                "last_indexed_code", "1970-01-01T00:00:00Z"
            )
            # Ensure timezone awareness for comparison if ISO strings have 'Z' or offset
            last_md_timestamp = datetime.datetime.fromisoformat(
                last_md_time_str.replace("Z", "+00:00")
            ).timestamp()
            last_code_timestamp = datetime.datetime.fromisoformat(
                last_code_time_str.replace("Z", "+00:00")
            ).timestamp()
            max_md_mod_timestamp = last_md_timestamp
            max_code_mod_timestamp = last_code_timestamp

            # Find all markdown files (only if auto-indexing is enabled)
            all_md_files_found = []
            # Check config at runtime after CLI has set it
            from ...core.config import DISABLE_AUTO_INDEXING

            if not DISABLE_AUTO_INDEXING:
                for md_file_path_str in glob.glob(
                    str(current_project_dir / "**/*.md"), recursive=True
                ):
                    md_path_obj = Path(md_file_path_str)
                    should_ignore = False
                    # Path component check from main.py:560-565
                    for part in md_path_obj.parts:
                        if part in IGNORE_DIRS_FOR_INDEXING or (
                            part.startswith(".") and part not in [".", ".."]
                        ):
                            should_ignore = True
                            break
                    if not should_ignore:
                        all_md_files_found.append(md_path_obj)

                logger.info(
                    f"Found {len(all_md_files_found)} markdown files to consider for indexing (after filtering ignored dirs)."
                )
            else:
                logger.info(
                    "Automatic markdown indexing disabled. Skipping markdown file scanning."
                )

            # Find all code files (only in advanced mode)
            all_code_files_found = []
            if _emb_settings.advanced:
                for extension in CODE_EXTENSIONS:
                    for code_file_path_str in glob.glob(
                        str(current_project_dir / f"**/*{extension}"), recursive=True
                    ):
                        code_path_obj = Path(code_file_path_str)
                        should_ignore = False
                        for part in code_path_obj.parts:
                            if part in IGNORE_DIRS_FOR_INDEXING or (
                                part.startswith(".") and part not in [".", ".."]
                            ):
                                should_ignore = True
                                break
                        if not should_ignore:
                            all_code_files_found.append(code_path_obj)

                logger.info(
                    f"Found {len(all_code_files_found)} code files to consider for indexing (after filtering ignored dirs)."
                )

            # Process markdown files (only if auto-indexing is enabled)
            if not DISABLE_AUTO_INDEXING:
                for md_path_obj in all_md_files_found:
                    try:
                        mod_time = md_path_obj.stat().st_mtime
                        content = md_path_obj.read_text(encoding="utf-8")
                        normalized_path = str(
                            md_path_obj.relative_to(current_project_dir).as_posix()
                        )
                        current_hash = hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest()
                        sources_to_check.append(
                            (
                                "markdown",
                                normalized_path,
                                content,
                                mod_time,
                                current_hash,
                            )
                        )
                        if mod_time > max_md_mod_timestamp:
                            max_md_mod_timestamp = mod_time
                    except Exception as e:
                        logger.warning(
                            f"Failed to read or process markdown file {md_path_obj}: {e}"
                        )

            # Process code files
            for code_path_obj in all_code_files_found:
                try:
                    mod_time = code_path_obj.stat().st_mtime
                    content = code_path_obj.read_text(encoding="utf-8")
                    normalized_path = str(
                        code_path_obj.relative_to(current_project_dir).as_posix()
                    )
                    current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    sources_to_check.append(
                        ("code", normalized_path, content, mod_time, current_hash)
                    )
                    if mod_time > max_code_mod_timestamp:
                        max_code_mod_timestamp = mod_time
                except Exception as e:
                    logger.warning(
                        f"Failed to read or process code file {code_path_obj}: {e}"
                    )

            # 2. Scan Project Context (Original main.py:585-603)
            last_ctx_time_str = last_indexed_timestamps.get(
                "last_indexed_context", "1970-01-01T00:00:00Z"
            )
            max_ctx_mod_time_iso = (
                last_ctx_time_str  # Keep as ISO string for direct comparison
            )

            # Phase 7b renamed last_updated -> updated_at on project_context.
            cursor.execute(
                "SELECT context_key, value, description, updated_at FROM project_context WHERE updated_at > ?",
                (last_ctx_time_str,),
            )
            for row in cursor.fetchall():
                key = row["context_key"]
                # ADR-0017 (Wave 12 PR B): every project_context row is
                # indexed — no content-based secret skip. memory is shared
                # project knowledge; real secrets belong in the
                # operator-only, non-RAG project_settings store.
                value_str = row["value"]  # Already a JSON string in DB
                desc = row["description"] or ""
                last_mod_iso = row["updated_at"]
                # Content for hashing and embedding (main.py:593-595)
                content_for_embedding = (
                    f"Context Key: {key}\nDescription: {desc}\nValue: {value_str}"
                )
                current_hash = hashlib.sha256(
                    content_for_embedding.encode("utf-8")
                ).hexdigest()
                sources_to_check.append(
                    ("context", key, content_for_embedding, last_mod_iso, current_hash)
                )
                if last_mod_iso > max_ctx_mod_time_iso:
                    max_ctx_mod_time_iso = last_mod_iso

            # 3. Scan File Metadata (Original main.py:605 - "Skipped for now") - Still skipped.

            # 4. Scan Tasks (only in advanced mode - For System 8)
            max_task_mod_time_iso = last_indexed_timestamps.get(
                "last_indexed_tasks", "1970-01-01T00:00:00Z"
            )

            if _emb_settings.advanced:
                last_task_time_str = last_indexed_timestamps.get(
                    "last_indexed_tasks", "1970-01-01T00:00:00Z"
                )

                # Get tasks that have been updated since last indexing
                cursor.execute(
                    "SELECT task_id, title, description, status, assigned_to, created_by, "
                    "parent_task, depends_on_tasks, priority, created_at, updated_at "
                    "FROM tasks WHERE updated_at > ?",
                    (last_task_time_str,),
                )

                for task_row in cursor.fetchall():
                    task_data = dict(task_row)
                    task_id = task_data["task_id"]
                    last_mod_iso = task_data["updated_at"]

                    # Format task for embedding
                    content_for_embedding = format_task_for_embedding(task_data)
                    current_hash = hashlib.sha256(
                        content_for_embedding.encode("utf-8")
                    ).hexdigest()

                    sources_to_check.append(
                        (
                            "task",
                            task_id,
                            content_for_embedding,
                            last_mod_iso,
                            current_hash,
                        )
                    )

                    if last_mod_iso > max_task_mod_time_iso:
                        max_task_mod_time_iso = last_mod_iso

            # Map (source_type, source_ref) -> the row's mod-time (float
            # st_mtime for markdown/code, ISO ``updated_at`` for
            # context/task). Consulted at the watermark-advance step so
            # the incremental high-water mark is never pushed past a row
            # that failed to embed this cycle (BL-R31-1).
            source_mod_time: Dict[Tuple[str, str], Any] = {
                (s_type, s_ref): mod_time
                for s_type, s_ref, _content, mod_time, _hash in sources_to_check
            }
            # Populated after embedding: sources whose EVERY chunk
            # embedded (safe to advance past) vs. sources with >=1 failed
            # chunk (must be re-selected next cycle). Default empty so the
            # watermark logic below is a straight no-op when nothing was
            # embedded this cycle.
            fully_embedded_sources: set = set()
            sources_with_failed_chunks: set = set()

            # Filter sources based on hash comparison (Original main.py:608-615)
            sources_to_process_for_embedding: List[Tuple[str, str, str, str]] = (
                []
            )  # type, ref, content, current_hash
            for source_type, source_ref, content, _, current_hash in sources_to_check:
                meta_key_for_hash = f"hash_{source_type}_{source_ref}"
                stored_source_hash = stored_hashes.get(meta_key_for_hash)
                if current_hash != stored_source_hash:
                    logger.info(
                        f"Change detected for {source_type}: {source_ref} (Hash mismatch or new). Queued for re-indexing."
                    )
                    sources_to_process_for_embedding.append(
                        (source_type, source_ref, content, current_hash)
                    )
                # else: logger.debug(f"No change for {source_type}:{source_ref} (hash match)")

            if not sources_to_process_for_embedding:
                logger.info(
                    "No new or modified sources found requiring RAG index update."
                )
            else:
                logger.info(
                    f"Processing {len(sources_to_process_for_embedding)} updated/new sources for RAG index."
                )

                processed_hashes_to_update_in_meta: Dict[str, str] = {}

                # Delete existing chunks for sources needing update.
                # PR F: rag_repo.delete_chunks_for owns the
                # embeddings-then-chunks delete ordering and the
                # rag_embeddings-existence guard. We pass the
                # cycle's shared ``cursor`` so the deletes join the
                # same transaction as the inserts further down.
                #
                # R10-F2: the commit below is UNCONDITIONAL — see
                # ``_delete_stale_chunks_and_commit``'s docstring for
                # why gating it on "did anything actually get deleted"
                # left a write transaction open (and the WAL write
                # lock held) across the entire embedding-await phase
                # that follows, starving every other writer in the
                # process past its busy_timeout.
                logger.info(
                    "Deleting existing chunks and embeddings for sources needing update..."
                )
                delete_count = _delete_stale_chunks_and_commit(
                    cursor, conn, sources_to_process_for_embedding,
                )

                # Generate chunks and prepare for embedding (Original main.py:631-647)
                all_chunks_texts_to_embed: List[str] = []
                chunk_source_metadata_map: List[
                    Tuple[str, str, str, Dict[str, Any]]
                ] = []  # type, ref, current_hash, metadata for each chunk

                # _emb_settings was resolved once at the top of this cycle.

                for (
                    source_type,
                    source_ref,
                    content,
                    current_hash_of_source,
                ) in sources_to_process_for_embedding:
                    chunks_with_metadata: List[Tuple[str, Dict[str, Any]]] = []

                    if _emb_settings.advanced:
                        # Advanced mode: Use sophisticated chunking
                        if source_type == "markdown":
                            # Markdown-aware chunking
                            text_chunks = markdown_aware_chunker(content)
                            chunks_with_metadata = [
                                (chunk, {"source_type": "markdown"})
                                for chunk in text_chunks
                            ]
                        elif source_type == "code":
                            # Code-aware chunking for code files
                            file_path = current_project_dir / source_ref

                            # First, create a file summary
                            entities = extract_code_entities(content, file_path)
                            file_summary = create_file_summary(
                                content, file_path, entities
                            )
                            summary_text = f"File: {source_ref}\n{json.dumps(file_summary, indent=2)}"
                            chunks_with_metadata.append(
                                (
                                    summary_text,
                                    {"source_type": "code_summary", **file_summary},
                                )
                            )

                            # Then chunk the code
                            code_chunks = chunk_code_aware(content, file_path)
                            chunks_with_metadata.extend(code_chunks)
                        else:
                            # Simple chunking for other types
                            text_chunks = simple_chunker(content)
                            chunks_with_metadata = [
                                (chunk, {"source_type": source_type})
                                for chunk in text_chunks
                            ]
                    else:
                        # Original/Simple mode: Basic chunking for all types
                        text_chunks = simple_chunker(content)
                        # Store minimal metadata
                        chunks_with_metadata = [
                            (chunk, {"source_type": source_type})
                            for chunk in text_chunks
                        ]

                    if not chunks_with_metadata:
                        file_size = len(content) if content else 0
                        logger.warning(
                            f"No chunks generated for {source_type}: {source_ref} (file size: {file_size} bytes, likely empty or only whitespace). Skipping."
                        )
                        continue

                    for chunk_text, metadata in chunks_with_metadata:
                        # Validate chunk before adding - skip empty or whitespace-only chunks
                        if chunk_text and chunk_text.strip():
                            all_chunks_texts_to_embed.append(chunk_text.strip())
                            # Store metadata along with source info
                            chunk_source_metadata_map.append(
                                (
                                    source_type,
                                    source_ref,
                                    current_hash_of_source,
                                    metadata,
                                )
                            )
                        else:
                            logger.warning(
                                f"Skipping empty chunk from {source_type}: {source_ref}"
                            )

                if all_chunks_texts_to_embed:
                    logger.info(
                        f"Generated {len(all_chunks_texts_to_embed)} new chunks for embedding."
                    )

                    all_embeddings_vectors: List[Optional[List[float]]] = [None] * len(
                        all_chunks_texts_to_embed
                    )
                    embeddings_api_successful = (
                        True  # Flag to track overall success of API calls
                    )

                    # Parallel embedding processing (Original main.py:662-690)
                    embedding_api_call_start_time = time.time()
                    # Process batches in groups with controlled concurrency
                    for group_start_idx in range(
                        0,
                        len(all_chunks_texts_to_embed),
                        MAX_CONCURRENT_EMBEDDING_REQUESTS
                        * PARALLEL_EMBEDDING_BATCH_SIZE,
                    ):
                        # Determine how many batches to run in this parallel group
                        num_batches_in_group = 0
                        temp_idx = group_start_idx
                        while (
                            num_batches_in_group < MAX_CONCURRENT_EMBEDDING_REQUESTS
                            and temp_idx < len(all_chunks_texts_to_embed)
                        ):
                            num_batches_in_group += 1
                            temp_idx += PARALLEL_EMBEDDING_BATCH_SIZE

                        logger.info(
                            f"Processing up to {num_batches_in_group} embedding batches in parallel (group starting at chunk {group_start_idx})..."
                        )

                        try:
                            async with anyio.create_task_group() as tg_embed:
                                for i in range(num_batches_in_group):
                                    batch_actual_start_index = (
                                        group_start_idx
                                        + i * PARALLEL_EMBEDDING_BATCH_SIZE
                                    )
                                    if batch_actual_start_index >= len(
                                        all_chunks_texts_to_embed
                                    ):
                                        break  # No more chunks

                                    batch_end_index = min(
                                        batch_actual_start_index
                                        + PARALLEL_EMBEDDING_BATCH_SIZE,
                                        len(all_chunks_texts_to_embed),
                                    )
                                    current_batch_chunks = all_chunks_texts_to_embed[
                                        batch_actual_start_index:batch_end_index
                                    ]

                                    if not current_batch_chunks:
                                        continue

                                    tg_embed.start_soon(
                                        _get_embeddings_batch,
                                        current_batch_chunks,
                                        batch_actual_start_index,
                                        all_embeddings_vectors,
                                    )
                        except (
                            Exception
                        ) as e_tg:  # Catch errors from the task group itself
                            logger.error(
                                f"Error in parallel embedding batch processing task group: {e_tg}"
                            )
                            embeddings_api_successful = (
                                False  # Mark failure if task group fails
                            )

                        if not embeddings_api_successful:
                            break  # Stop if a task group failed

                        # Minimal delay between batch groups (Original main.py:689)
                        if (
                            group_start_idx
                            + MAX_CONCURRENT_EMBEDDING_REQUESTS
                            * PARALLEL_EMBEDDING_BATCH_SIZE
                            < len(all_chunks_texts_to_embed)
                        ):
                            await anyio.sleep(0.1)  # Reduced from 0.2

                    embedding_api_duration = time.time() - embedding_api_call_start_time
                    logger.info(
                        f"Completed all embedding API calls in {embedding_api_duration:.2f} seconds."
                    )

                    # Check for failed embeddings (None values)
                    failed_embedding_count = sum(
                        1 for emb_vec in all_embeddings_vectors if emb_vec is None
                    )
                    if failed_embedding_count > 0:
                        logger.warning(
                            f"{failed_embedding_count} out of {len(all_embeddings_vectors)} embeddings failed to generate."
                        )
                        # If a significant portion failed, mark the overall API call as unsuccessful
                        if (
                            failed_embedding_count > len(all_embeddings_vectors) // 2
                        ):  # More than half failed
                            embeddings_api_successful = False
                            logger.error(
                                "More than half of the embeddings failed. Marking RAG indexing cycle for these sources as unsuccessful."
                            )

                    # Per-source embedding outcome (BL-R31-1). A source
                    # row counts as fully embedded only when EVERY one of
                    # its chunks produced a vector. A row with any failed
                    # chunk must NOT advance its hash and must hold the
                    # incremental watermark back, or the next cycle's
                    # ``updated_at > watermark`` scan would never re-select
                    # it and ask_project_rag would serve it stale forever.
                    source_total_chunks: Dict[Tuple[str, str], int] = {}
                    source_failed_chunks: Dict[Tuple[str, str], int] = {}
                    for chunk_idx, (
                        s_type,
                        s_ref,
                        _s_hash,
                        _s_meta,
                    ) in enumerate(chunk_source_metadata_map):
                        src_key = (s_type, s_ref)
                        source_total_chunks[src_key] = (
                            source_total_chunks.get(src_key, 0) + 1
                        )
                        if all_embeddings_vectors[chunk_idx] is None:
                            source_failed_chunks[src_key] = (
                                source_failed_chunks.get(src_key, 0) + 1
                            )
                    for src_key in source_total_chunks:
                        if source_failed_chunks.get(src_key, 0) == 0:
                            fully_embedded_sources.add(src_key)
                        else:
                            sources_with_failed_chunks.add(src_key)

                    # Insert new chunks and embeddings into DB.
                    # PR F: rag_repo.bulk_index_chunks owns the
                    # chunk + embedding INSERT pair and the
                    # rag_embeddings-existence guard. We call it per
                    # chunk so the per-iteration error tolerance and
                    # hash-on-success bookkeeping below stay
                    # byte-for-byte identical to the pre-flip loop.
                    if embeddings_api_successful:
                        logger.info(
                            "Inserting new chunks and embeddings into the database..."
                        )
                        inserted_count = 0
                        for i, chunk_text_to_insert in enumerate(
                            all_chunks_texts_to_embed
                        ):
                            embedding_vector = all_embeddings_vectors[i]
                            if embedding_vector is None:
                                logger.warning(
                                    f"Skipping chunk {i} for DB insertion due to missing embedding."
                                )
                                continue

                            (
                                source_type,
                                source_ref,
                                current_hash_of_source,
                                chunk_metadata,
                            ) = chunk_source_metadata_map[i]
                            n_written = rag_repo.bulk_index_chunks(
                                source_type=source_type,
                                source_ref=source_ref,
                                chunks=[{
                                    "chunk_text": chunk_text_to_insert,
                                    "embedding": embedding_vector,
                                    "metadata": chunk_metadata,
                                }],
                                connection=cursor,
                            )
                            if n_written > 0:
                                inserted_count += n_written
                                # Only advance the stored hash when the
                                # ENTIRE source embedded (BL-R31-1). A
                                # partially-embedded row keeps its old
                                # hash so the next cycle re-embeds it in
                                # full instead of leaving chunks missing.
                                if (
                                    source_type,
                                    source_ref,
                                ) in fully_embedded_sources:
                                    meta_key_for_hash_update = (
                                        f"hash_{source_type}_{source_ref}"
                                    )
                                    processed_hashes_to_update_in_meta[
                                        meta_key_for_hash_update
                                    ] = current_hash_of_source

                        logger.info(
                            f"Successfully inserted {inserted_count} new chunks/embeddings."
                        )

                        # Update rag_meta with the new hashes for
                        # successfully processed sources. PR F:
                        # rag_repo.set_meta groups per source_type;
                        # the in-cycle dict keys ``hash_<src>_<ref>``
                        # are split back into per-source groups and
                        # set_meta is called once per source_type so
                        # the repo owns the canonical key shape.
                        if processed_hashes_to_update_in_meta:
                            logger.info(
                                f"Updating {len(processed_hashes_to_update_in_meta)} source hashes in rag_meta..."
                            )
                            per_source: Dict[str, Dict[str, str]] = {}
                            for full_key, hash_val in (
                                processed_hashes_to_update_in_meta.items()
                            ):
                                # Strip the ``hash_`` prefix and split
                                # ``<source_type>_<source_ref>`` on
                                # the FIRST underscore — source_ref can
                                # itself contain underscores
                                # (``docs/sub_dir/file.md``) so a
                                # naive split would mis-attribute.
                                without_prefix = full_key[len("hash_"):]
                                source_type, _, source_ref = (
                                    without_prefix.partition("_")
                                )
                                per_source.setdefault(
                                    source_type, {}
                                )[source_ref] = hash_val
                            for source_type, mapping in per_source.items():
                                rag_repo.set_meta(
                                    source_type=source_type,
                                    source_hashes=mapping,
                                    connection=cursor,
                                )
                    else:
                        logger.warning(
                            "Skipping DB insertion and hash updates for this RAG cycle due to embedding API errors."
                        )

            # Update last indexed *timestamps* in rag_meta. Only
            # update if the embedding part (if attempted) was
            # successful or no embeddings were needed. The
            # ``embeddings_api_successful`` flag covers this.
            # PR F: rag_repo.set_meta owns the writes; the
            # auto-indexing / advanced-mode gates remain at the call
            # site because they're decisions about WHEN to update,
            # not about HOW to write the row.
            if (
                "embeddings_api_successful" not in locals() or embeddings_api_successful
            ):  # Check if flag exists and is True
                # Cap every watermark below the earliest row that failed
                # to embed this cycle so failed rows are re-scanned next
                # cycle (BL-R31-1). With no failures these collapse to the
                # original ``max_*`` values.
                if not DISABLE_AUTO_INDEXING:
                    capped_md_ts = _watermark_after_failures(
                        last_md_timestamp,
                        max_md_mod_timestamp,
                        "markdown",
                        source_mod_time,
                        fully_embedded_sources,
                        sources_with_failed_chunks,
                    )
                    new_md_time_iso = (
                        datetime.datetime.fromtimestamp(
                            capped_md_ts
                        ).isoformat()
                        + "Z"
                    )
                    rag_repo.set_meta(
                        source_type="markdown",
                        last_indexed_at=new_md_time_iso,
                        connection=cursor,
                    )
                capped_ctx_iso = _watermark_after_failures(
                    last_ctx_time_str,
                    max_ctx_mod_time_iso,
                    "context",
                    source_mod_time,
                    fully_embedded_sources,
                    sources_with_failed_chunks,
                )
                rag_repo.set_meta(
                    source_type="context",
                    last_indexed_at=capped_ctx_iso,
                    connection=cursor,
                )

                # Only update code and tasks timestamps in advanced mode
                if _emb_settings.advanced:
                    capped_code_ts = _watermark_after_failures(
                        last_code_timestamp,
                        max_code_mod_timestamp,
                        "code",
                        source_mod_time,
                        fully_embedded_sources,
                        sources_with_failed_chunks,
                    )
                    new_code_time_iso = (
                        datetime.datetime.fromtimestamp(
                            capped_code_ts
                        ).isoformat()
                        + "Z"
                    )
                    rag_repo.set_meta(
                        source_type="code",
                        last_indexed_at=new_code_time_iso,
                        connection=cursor,
                    )
                    # Task rows scan as source_type "task" (singular);
                    # the watermark meta key is "tasks".
                    capped_task_iso = _watermark_after_failures(
                        last_task_time_str,
                        max_task_mod_time_iso,
                        "task",
                        source_mod_time,
                        fully_embedded_sources,
                        sources_with_failed_chunks,
                    )
                    rag_repo.set_meta(
                        source_type="tasks",
                        last_indexed_at=capped_task_iso,
                        connection=cursor,
                    )
                # Add other source types here
            else:
                logger.warning(
                    "Skipping rag_meta timestamp updates due to errors in the embedding/indexing cycle."
                )

            conn.commit()  # Commit all DB changes for this cycle

            # Diagnostic query (Original main.py:740-747)
            try:
                diag_cursor = conn.cursor()  # Use a new cursor or the same one
                diag_cursor.execute("SELECT COUNT(*) FROM rag_chunks")
                chunk_count_diag = diag_cursor.fetchone()[0]
                diag_cursor.execute("SELECT COUNT(*) FROM rag_embeddings")
                embedding_count_diag = diag_cursor.fetchone()[0]
                logger.info(
                    f"DB RAG DIAGNOSTIC: Found {chunk_count_diag} chunks and {embedding_count_diag} embeddings post-cycle."
                )
            except Exception as e_diag:
                logger.error(f"Error running RAG database diagnostics: {e_diag}")

        except sqlite3.OperationalError as e_sqlite_op:  # main.py:750-753
            if (
                "no such module: vec0" in str(e_sqlite_op)
                or "vector search requires" in str(e_sqlite_op).lower()
            ):
                logger.warning(
                    f"Vector search module (vec0) not available or table missing. RAG indexing cycle skipped. Error: {e_sqlite_op}"
                )
                g.global_vss_load_successful = (
                    False  # Mark VSS as not usable if this happens
                )
            else:
                logger.error(
                    f"Database operational error in RAG indexing cycle: {e_sqlite_op}",
                    exc_info=True,
                )
        except Exception as e_cycle:  # main.py:756 (general catch-all for the cycle)
            logger.error(f"Error in RAG indexing cycle: {e_cycle}", exc_info=True)
        finally:
            if conn:
                conn.close()

        elapsed_cycle_time = time.time() - cycle_start_time
        logger.info(
            f"RAG index update cycle finished in {elapsed_cycle_time:.2f} seconds."
        )

        # Sleep interval (Original main.py:760)
        # Adjusted sleep: min 60s, or interval_seconds, whichever is larger.
        # The original had `max(30, interval_seconds / 5)` which could be very short.
        # Let's use a more stable sleep or make it configurable.
        # For 1-to-1, let's use the original logic:
        sleep_duration = max(30, interval_seconds // 5)
        logger.debug(f"RAG indexer sleeping for {sleep_duration} seconds.")
        await anyio.sleep(sleep_duration)

    logger.info("Background RAG indexer process stopped.")


# This function, run_rag_indexing_periodically, will be started as a background task
# by the server lifecycle management (e.g., in app/server_lifecycle.py).


# Task indexing functions for System 8
async def index_task_data(task_id: str, task_data: Dict[str, Any]) -> None:
    """
    Index a single task into the RAG system.

    Args:
        task_id: Task ID to index
        task_data: Complete task data dictionary
    """
    if not is_vss_loadable():
        logger.warning("Cannot index task - VSS not available")
        return

    # PR F: rag_repo owns the delete-then-insert pair for the per-task
    # re-index. We still hold the connection so the OpenAI embedding
    # call (which can take several seconds) doesn't pin a transaction;
    # delete first standalone, then bulk-index the freshly-embedded
    # chunks via the repo on its own commit.
    from ...repositories import rag_repo

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Format task for embedding
        content = format_task_for_embedding(task_data)

        # Generate chunks (tasks are usually small, so one chunk is fine)
        chunks = simple_chunker(content, chunk_size=2000)

        # Delete existing chunks for this task. Shares the owner
        # cursor so the delete stages on this connection, but MUST
        # commit right away (R10-F2, same class as
        # ``_delete_stale_chunks_and_commit`` in
        # ``run_rag_indexing_periodically``): the DELETE opens sqlite's
        # write transaction — and holds its WAL write lock — the
        # instant it executes, whether or not it matched any rows (a
        # never-before-indexed task matches 0). Without this commit
        # the lock stayed held across the embedding call(s) below,
        # which is exactly the multi-second-under-load network hop the
        # comment above says we're trying not to pin a transaction
        # across — starving every other writer past its busy_timeout.
        rag_repo.delete_chunks_for("task", task_id, connection=cursor)
        conn.commit()

        # Embedding seam: one endpoint-resolution rule shared with the
        # periodic indexer + the query path.
        emb_client = embedding_client()

        # Generate embeddings for each chunk + insert via repo.
        for chunk_text in chunks:
            try:
                embedding_vector = emb_client.embed([chunk_text])[0]
                rag_repo.bulk_index_chunks(
                    source_type="task",
                    source_ref=task_id,
                    chunks=[{
                        "chunk_text": chunk_text,
                        "embedding": embedding_vector,
                        "metadata": None,
                    }],
                    connection=cursor,
                )
            except Exception as e:
                logger.error(f"Error generating embedding for task {task_id}: {e}")

        conn.commit()
        logger.info(f"Successfully indexed task {task_id}")

    except Exception as e:
        logger.error(f"Error indexing task {task_id}: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


async def index_all_tasks() -> None:
    """Index all tasks from the database."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all tasks
        cursor.execute(
            "SELECT task_id, title, description, status, assigned_to, created_by, "
            "parent_task, depends_on_tasks, priority, created_at, updated_at "
            "FROM tasks"
        )

        tasks = cursor.fetchall()
        logger.info(f"Indexing {len(tasks)} tasks for RAG")

        for task_row in tasks:
            task_data = dict(task_row)
            # Parse JSON fields
            if task_data.get("depends_on_tasks"):
                try:
                    task_data["depends_on_tasks"] = json.loads(
                        task_data["depends_on_tasks"]
                    )
                except json.JSONDecodeError:
                    task_data["depends_on_tasks"] = []

            await index_task_data(task_data["task_id"], task_data)

        # Update last indexed time via rag_repo (PR F).
        from ...repositories import rag_repo
        rag_repo.set_meta(
            source_type="tasks",
            last_indexed_at=datetime.datetime.now().isoformat(),
            connection=cursor,
        )
        conn.commit()

    except Exception as e:
        logger.error(f"Error indexing all tasks: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


def format_task_for_embedding(task_data: Dict[str, Any]) -> str:
    """
    Format task data into text suitable for embedding.

    Args:
        task_data: Task data dictionary

    Returns:
        Formatted text for embedding
    """
    parts = [
        f"Task ID: {task_data.get('task_id', 'unknown')}",
        f"Title: {task_data.get('title', 'Untitled')}",
        f"Description: {task_data.get('description', 'No description')}",
        f"Status: {task_data.get('status', 'unknown')}",
        f"Priority: {task_data.get('priority', 'medium')}",
        f"Assigned to: {task_data.get('assigned_to', 'unassigned')}",
        f"Created by: {task_data.get('created_by', 'unknown')}",
    ]

    if task_data.get("parent_task"):
        parts.append(f"Parent task: {task_data['parent_task']}")
    else:
        parts.append("Parent task: None (root level)")

    depends_on = task_data.get("depends_on_tasks", [])
    if isinstance(depends_on, str):
        try:
            depends_on = json.loads(depends_on)
        except:
            depends_on = []

    if depends_on:
        parts.append(f"Dependencies: {', '.join(depends_on)}")
    else:
        parts.append("Dependencies: None")

    # Add metadata
    parts.extend(
        [
            f"Created at: {task_data.get('created_at', 'unknown')}",
            f"Updated at: {task_data.get('updated_at', 'unknown')}",
        ]
    )

    return "\n".join(parts)
