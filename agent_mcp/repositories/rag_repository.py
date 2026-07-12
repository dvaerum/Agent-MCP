# Agent-MCP/agent_mcp/repositories/rag_repository.py
"""RagRepository — single owner of rag_chunks, rag_embeddings, rag_meta.

PR F of the round-2 architecture-review series — the **final PR**.
Round 2 already established the class-based Repository pattern for the
other three concept tables (Task #146, Agent #147, Message #155). RAG
was the last concept whose persistence was hand-rolled cursor work
scattered across :mod:`agent_mcp.features.rag` — :mod:`indexing` and
:mod:`query` each owned their own ``INSERT``/``SELECT``/``DELETE``
against the same three tables, the sqlite-vec dialect (``vec0``
virtual table, ``MATCH ?``, ``k = ?``, ``ORDER BY distance``) was
duplicated in two places, and ``db/actions/rag_db.py`` was an empty
placeholder. This class fixes all three.

Why one repository covers both ingest and search (Option 1):

* The methods cluster in two groups but **share a table**. Splitting
  along ingest/search would put per-table invariants (the
  ``rag_chunks.rowid == rag_embeddings.rowid`` link, the
  ``rag_meta`` last-indexed seed) on opposite sides of the seam — a
  re-index flow that today is ``DELETE FROM rag_embeddings WHERE
  rowid IN (SELECT chunk_id FROM rag_chunks ...) ; DELETE FROM
  rag_chunks ...`` would have to cross two repositories.
* The codebase already has three repos (Task/Agent/Message);
  consistency wins. The empty ``db/actions/rag_db.py`` stopped being a
  placeholder here (became a re-export shim) and was later deleted
  outright by arch-deepening R3 #2b once nothing needed the extra hop.
* The vector-search dialect (``vec0`` MATCH syntax, ``k = ?`` knn
  bind, ``ORDER BY distance``) lives in **one place**. Callers
  describe what they want (``search_similar(query_embedding, *,
  limit=, source_type_filter=)``) — they never spell the SQL.

The class methods split into two surfaces:

* **Ingest** — ``bulk_index_chunks``, ``delete_chunks_for``,
  ``set_meta``, ``get_last_indexed``. Used by the indexer's
  periodic cycle (:func:`run_rag_indexing_periodically`) and the
  per-task indexer (:func:`index_task_data`).
* **Search** — ``search_similar``, ``get_chunk_by_id``,
  ``fetch_recent_context``. Used by the query path
  (:func:`query_rag_system`,
  :func:`query_rag_system_with_model`).

EventBus parity: unlike :class:`MessageRepository`, the RAG concept
has no subscriber today — indexing is a polling cycle, search is
synchronous. There is no publish surface, and adding one would just
spam subscribers with chunks they don't consume. If a future PR adds
an "index updated" event (e.g. for the dashboard to live-refresh the
"last indexed" panels), the seam is ``_publish()`` below — the lazy-
import wrapper is already in place mirroring the pattern PR #153/#154/
#155 needed when they hit the circular-import hazard.

sqlite-vec gating: ``rag_embeddings`` is a ``vec0`` virtual table.
When the extension fails to load (``g.global_vss_load_successful``
False), search returns ``[]`` and ingest still writes the chunk rows
but skips the companion embedding. This mirrors the indexer's existing
``is_vss_loadable()`` gate so a host without the extension stays
functional for the rest of the application.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any, Dict, List, Optional

from ..core import globals as g
from ..core.config import logger
from ..db.connection import get_db_connection


def _publish(event: str, payload: Dict[str, Any]) -> None:  # pragma: no cover
    """Lazy-import shim around ``_event_bus_shim.publish``.

    No call site uses this today — RAG has no subscriber and the
    publish would just spam subscribers with chunks they don't
    consume. It exists for parity with :func:`MessageRepository._publish`
    and to mark the seam where a future "index updated" event would
    plug in. The lazy import shape mirrors PR #153/#154/#155; the
    original cycle it dodged (eagerly importing the legacy
    module-of-functions ``core.repositories`` package, which imported
    ``db.actions.*``, which re-exported from this module) was closed
    when arch-deepening R3 #2a deleted those shadow modules. Kept lazy
    here anyway for parity with the other repos' ``_publish`` and to
    avoid re-litigating import order on every relocation.
    """
    from ..core import event_bus_shim

    event_bus_shim.publish("rag", event, payload)


# ---------------------------------------------------------------------------
# Module-level helpers — formerly lived as raw INSERT/UPDATE blocks
# inside ``features/rag/indexing.py`` and SELECT blocks inside
# ``features/rag/query.py``. ``db/actions/rag_db.py`` used to
# re-export these for legacy callers; arch-deepening R3 #2b deleted
# that shim (``features/rag/indexing.py`` now imports directly).
#
# Behaviour is byte-for-byte identical to the pre-PR cursor work:
# same column order, same MATCH+k+ORDER BY clause, same handling of
# missing virtual table.
# ---------------------------------------------------------------------------


def _chunk_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Project a ``rag_chunks`` row into the dict shape consumers expect.

    Parses the optional ``metadata`` JSON column eagerly — every
    caller today did the same ``json.loads(row['metadata'])`` dance,
    so doing it once here removes a footgun (silent ``None`` when
    the body was a JSON string the caller forgot to parse).
    """
    out: Dict[str, Any] = {
        "chunk_id": row["chunk_id"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "chunk_text": row["chunk_text"],
        "indexed_at": row["indexed_at"],
        "metadata": None,
    }
    raw_meta = row["metadata"] if "metadata" in row.keys() else None
    if raw_meta:
        try:
            out["metadata"] = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            out["metadata"] = None
    return out


def _embeddings_table_exists(cursor: sqlite3.Cursor) -> bool:
    """Check whether the ``rag_embeddings`` virtual table is in the schema.

    The schema bootstrap creates it conditionally on
    ``check_vss_loadability()``; on a host without sqlite-vec the
    table is absent and any ``INSERT INTO rag_embeddings`` would
    raise ``OperationalError: no such table``. Every legacy caller
    today guards with this exact check; we centralise it once.
    """
    cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'virtual') AND name='rag_embeddings'"
    )
    return cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Secret redaction — owned by the retrieval SEAM.
#
# ``search_similar`` and ``fetch_recent_context`` are the single seam the
# RAG query path reads through. The seam that owns "what you get back"
# must own "with secrets removed" — otherwise any NEW caller of these
# methods echoes project_context secrets (``config_*_token`` etc.) to the
# LLM/worker, bypassing ``view_project_context``'s redaction. This used to
# be re-applied by hand at every ``features/rag/query.py`` call site; it
# now lives here so the guarantee travels with the data.
#
# Both helpers import lazily (not at module top) to sidestep the import
# cycle: ``tools/__init__`` -> ``rag_tools`` -> ``features/rag/query`` ->
# ``repositories`` -> this module, and ``features/rag/indexing`` also
# reaches back into ``repositories``. Deferring the import to call time
# keeps module load acyclic — the same shape ``features/rag/query`` uses.
# ---------------------------------------------------------------------------


def _is_secret_key(key: Optional[str]) -> bool:
    """Lazy wrapper over the canonical secret-key policy.

    Single source of truth is
    :func:`agent_mcp.tools.project_context_tools.is_secret_key`; every
    project_context surface (tool boundary, RAG index + query, dashboard
    composition) applies the SAME rule so they can't drift.
    """
    from ..tools.project_context_tools import is_secret_key

    return is_secret_key(key)


def _value_has_embedded_secret(*texts: Optional[str]) -> bool:
    """Lazy wrapper over the canonical embedded-secret VALUE scanner.

    Catches a credential stored in the VALUE of a non-secret-named key
    (e.g. ``deploy_notes`` holding a GitHub PAT). Reused (not duplicated)
    from the index path so the retrieval filter applies the SAME skip the
    indexer does.
    """
    from ..features.rag.indexing import _value_has_embedded_secret as _scan

    return _scan(*texts)


def _drop_secret_chunks(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop retrieved vector chunks that carry a credential — for ANY
    source type.

    Defense-in-depth against a STALE index that embedded a secret before
    the ``bulk_index_chunks`` choke-point existed. Two drop rules, both
    retained:

    * secret-KEYED ``project_context`` row — the chunk's ``source_ref``
      is the context_key for ``source_type == "context"`` (the original
      round-2 rule).
    * secret in the chunk TEXT itself — a credential pasted into a task
      description/title, a code file, or a markdown doc (R2-F3). This is
      keyed on the ``chunk_text`` VALUE, so it covers every source type,
      not just ``context``.
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


class RagRepository:
    """The class behind ``agent_mcp.repositories.rag_repo``.

    Instances are cheap and stateless — every method opens a fresh
    ``sqlite3`` connection via :func:`get_db_connection` (which handles
    sqlite-vec extension loading per connection). The class identity
    exists so callers can hold a reference, type-check against
    ``RagRepository``, and (in future PRs) attach per-instance state
    without rewriting every call site.

    No in-memory cache today — every read goes straight to the DB.
    Vector search is itself a kind of cache; an additional Python-side
    layer would just complicate invalidation.
    """

    # --- Ingest side ----------------------------------------------------

    def bulk_index_chunks(
        self,
        source_type: str,
        source_ref: str,
        chunks: List[Dict[str, Any]],
        *,
        connection: Any = None,
    ) -> int:
        """Insert N chunks + their embeddings atomically. Returns count.

        Each ``chunks`` entry is a dict with keys:

        * ``chunk_text`` (str) — the text body
        * ``embedding`` (list[float] | None) — the embedding vector
          for this chunk; when ``None``, only the chunk row is
          written (matches the indexer's behaviour when the embedding
          API returned None for that slot)
        * ``metadata`` (dict | None) — optional per-chunk metadata;
          stored as JSON

        ``connection`` is the transaction-aware seam. Tolerates a raw
        ``sqlite3.Cursor`` so the indexer's per-cycle ``conn`` can
        keep its existing single end-of-cycle commit. When ``None``,
        the method opens its own connection and commits.

        On a host where ``rag_embeddings`` is absent (sqlite-vec not
        loaded), the chunk rows still land but the embedding rows are
        silently skipped — same as the indexer's degraded path today.
        """
        if not chunks:
            return 0

        indexed_at = datetime.datetime.now().isoformat()
        external_conn = connection is not None

        if external_conn:
            cursor = connection
            owns_conn = None
        else:
            owns_conn = get_db_connection()
            cursor = owns_conn.cursor()

        inserted = 0
        try:
            has_embeddings_table = _embeddings_table_exists(cursor)
            for chunk in chunks:
                chunk_text = chunk.get("chunk_text")
                if not chunk_text:
                    continue
                # SECURITY (R2-F3): the ingest CHOKE-POINT. Scan every
                # chunk's text for an embedded credential — for ALL source
                # types, not just ``context`` — and skip a secret-bearing
                # chunk so it is never embedded. Any agent with rag.query
                # can retrieve indexed chunks via ask_project_rag, so a
                # token pasted into a task description/title, a code file,
                # or a markdown doc would otherwise be echoed to the LLM.
                # This one seam covers the periodic indexer, index_task_data
                # and any future ingester. Reuses the same value scanner
                # the context path uses so the policy can't drift. The
                # high-entropy heuristic may over-drop a chunk holding a
                # long hash (e.g. a git SHA in code) — an accepted trade
                # for closing a live secret-exfiltration.
                if _value_has_embedded_secret(chunk_text):
                    logger.warning(
                        "Skipping RAG embedding for %s:%s chunk: text "
                        "matched an embedded-secret pattern.",
                        source_type, source_ref,
                    )
                    continue
                metadata = chunk.get("metadata")
                metadata_json = json.dumps(metadata) if metadata else None
                cursor.execute(
                    "INSERT INTO rag_chunks "
                    "(source_type, source_ref, chunk_text, indexed_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source_type, source_ref, chunk_text, indexed_at,
                     metadata_json),
                )
                chunk_rowid = cursor.lastrowid

                embedding = chunk.get("embedding")
                if embedding is not None and has_embeddings_table:
                    embedding_json = json.dumps(embedding)
                    cursor.execute(
                        "INSERT INTO rag_embeddings (rowid, embedding) "
                        "VALUES (?, ?)",
                        (chunk_rowid, embedding_json),
                    )
                inserted += 1

            if owns_conn is not None:
                owns_conn.commit()
        except sqlite3.Error as e:
            logger.error(
                f"Database error bulk-indexing {len(chunks)} chunks for "
                f"{source_type}:{source_ref}: {e}",
                exc_info=True,
            )
            if owns_conn is not None:
                try:
                    owns_conn.rollback()
                except sqlite3.Error:
                    pass
            return 0
        finally:
            if owns_conn is not None:
                owns_conn.close()

        return inserted

    def delete_chunks_for(
        self,
        source_type: str,
        source_ref: str,
        *,
        connection: Any = None,
    ) -> int:
        """Remove every chunk + matching embedding for a source.

        Returns the count of ``rag_chunks`` rows deleted. The
        embedding row is keyed by ``rowid``, so the delete order
        matters — embeddings first, then chunks — otherwise the
        sub-select can't resolve the rowids of rows that just got
        purged. This is the same ordering the indexer's re-index
        loop uses.

        On a host where ``rag_embeddings`` is absent, the embeddings
        delete is silently skipped.
        """
        external_conn = connection is not None
        if external_conn:
            cursor = connection
            owns_conn = None
        else:
            owns_conn = get_db_connection()
            cursor = owns_conn.cursor()

        try:
            if _embeddings_table_exists(cursor):
                cursor.execute(
                    "DELETE FROM rag_embeddings WHERE rowid IN ("
                    "  SELECT chunk_id FROM rag_chunks "
                    "  WHERE source_type = ? AND source_ref = ?"
                    ")",
                    (source_type, source_ref),
                )
            result = cursor.execute(
                "DELETE FROM rag_chunks "
                "WHERE source_type = ? AND source_ref = ?",
                (source_type, source_ref),
            )
            count = result.rowcount or 0
            if owns_conn is not None:
                owns_conn.commit()
            return count
        except sqlite3.Error as e:
            logger.error(
                f"Database error deleting chunks for "
                f"{source_type}:{source_ref}: {e}",
                exc_info=True,
            )
            if owns_conn is not None:
                try:
                    owns_conn.rollback()
                except sqlite3.Error:
                    pass
            return 0
        finally:
            if owns_conn is not None:
                owns_conn.close()

    def purge_source(
        self,
        source_type: str,
        source_ref: str,
        *,
        connection: Any = None,
    ) -> int:
        """Hard-evict a source from the RAG index on row delete.

        Deletes the source's chunks + embeddings (via
        :meth:`delete_chunks_for`) AND clears its
        ``hash_<source_type>_<source_ref>`` ``rag_meta`` watermark.
        Returns the count of ``rag_chunks`` rows deleted.

        Why both, together: the incremental indexer keys on
        ``updated_at`` and only re-touches a source when its content
        hash differs from the stored ``hash_*`` row. Deleting the
        underlying task/context row leaves no future ``updated_at`` to
        trip that comparison, so a stale chunk (and the secret-free but
        deleted content it holds) would live forever and keep surfacing
        through ``ask_project_rag``. Clearing the hash also means a
        future re-add of the SAME key re-indexes instead of being
        skipped as "unchanged" against a ghost hash.

        Distinct from :meth:`delete_chunks_for`, which the indexer uses
        mid-cycle and MUST NOT clear the hash (it re-inserts the chunk
        and re-sets the hash in the same cycle).

        ``connection`` is the transaction-aware seam — pass the caller's
        cursor so the purge joins the same transaction as the row
        delete. When ``None``, opens its own connection and commits.

        Robustness note: the chunk-row delete is what actually stops
        ``ask_project_rag`` surfacing the content (its kNN inner-joins
        ``rag_embeddings`` to ``rag_chunks`` on ``chunk_id`` — a chunk-
        less embedding row can't project any text). So the embeddings
        delete is best-effort: if the ``vec0`` virtual table is
        registered in the schema but its module isn't loaded on this
        connection, the embeddings ``DELETE`` raises but MUST NOT abort
        the chunk delete. sqlite3 keeps the transaction usable after a
        per-statement error, so we swallow it and press on.
        """
        external_conn = connection is not None
        if external_conn:
            cursor = connection
            owns_conn = None
        else:
            owns_conn = get_db_connection()
            cursor = owns_conn.cursor()

        try:
            if _embeddings_table_exists(cursor):
                try:
                    cursor.execute(
                        "DELETE FROM rag_embeddings WHERE rowid IN ("
                        "  SELECT chunk_id FROM rag_chunks "
                        "  WHERE source_type = ? AND source_ref = ?"
                        ")",
                        (source_type, source_ref),
                    )
                except sqlite3.OperationalError as e_vec:
                    # vec0 registered but not loaded on this connection —
                    # orphan embedding is harmless (no chunk to join to);
                    # the chunk delete below is the security-relevant one.
                    logger.warning(
                        "purge_source: embeddings delete skipped for "
                        "%s:%s (%s); continuing with chunk delete.",
                        source_type, source_ref, e_vec,
                    )
            result = cursor.execute(
                "DELETE FROM rag_chunks "
                "WHERE source_type = ? AND source_ref = ?",
                (source_type, source_ref),
            )
            count = result.rowcount or 0
            cursor.execute(
                "DELETE FROM rag_meta WHERE meta_key = ?",
                (f"hash_{source_type}_{source_ref}",),
            )
            if owns_conn is not None:
                owns_conn.commit()
            return count
        except sqlite3.Error as e:
            logger.error(
                f"Database error purging RAG source "
                f"{source_type}:{source_ref}: {e}",
                exc_info=True,
            )
            if owns_conn is not None:
                try:
                    owns_conn.rollback()
                except sqlite3.Error:
                    pass
            return 0
        finally:
            if owns_conn is not None:
                owns_conn.close()

    def set_meta(
        self,
        *,
        source_type: str,
        last_indexed_at: Optional[str] = None,
        source_hashes: Optional[Dict[str, str]] = None,
        connection: Any = None,
    ) -> None:
        """Update the ``rag_meta`` rows for a source type.

        Two distinct write surfaces share a single method because the
        indexer always updates both in lockstep at end-of-cycle:

        * ``last_indexed_at`` → writes the
          ``last_indexed_<source_type>`` row
        * ``source_hashes`` → writes one
          ``hash_<source_type>_<source_ref>`` row per entry

        Either is optional; if both are ``None`` the call is a no-op.
        ``INSERT OR REPLACE`` semantics match the indexer's
        ``executemany`` today.
        """
        if last_indexed_at is None and not source_hashes:
            return

        external_conn = connection is not None
        if external_conn:
            cursor = connection
            owns_conn = None
        else:
            owns_conn = get_db_connection()
            cursor = owns_conn.cursor()

        try:
            if last_indexed_at is not None:
                cursor.execute(
                    "INSERT OR REPLACE INTO rag_meta "
                    "(meta_key, meta_value) VALUES (?, ?)",
                    (f"last_indexed_{source_type}", last_indexed_at),
                )
            if source_hashes:
                payload = [
                    (f"hash_{source_type}_{ref}", h)
                    for ref, h in source_hashes.items()
                ]
                cursor.executemany(
                    "INSERT OR REPLACE INTO rag_meta "
                    "(meta_key, meta_value) VALUES (?, ?)",
                    payload,
                )
            if owns_conn is not None:
                owns_conn.commit()
        except sqlite3.Error as e:
            logger.error(
                f"Database error updating rag_meta for {source_type}: {e}",
                exc_info=True,
            )
            if owns_conn is not None:
                try:
                    owns_conn.rollback()
                except sqlite3.Error:
                    pass
        finally:
            if owns_conn is not None:
                owns_conn.close()

    def get_last_indexed(self, source_type: str) -> Optional[str]:
        """Return the ``last_indexed_<source_type>`` value, or ``None``
        if there is no row.

        Note the schema bootstrap seeds the canonical epoch baseline
        (``1970-01-01T00:00:00Z``) for every known source type, so a
        fresh DB always has a row. ``None`` is the "truly unknown
        source type" signal.
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT meta_value FROM rag_meta WHERE meta_key = ?",
                (f"last_indexed_{source_type}",),
            )
            row = cursor.fetchone()
            return row["meta_value"] if row is not None else None
        except sqlite3.Error as e:
            logger.error(
                f"Database error reading last_indexed for {source_type}: {e}",
                exc_info=True,
            )
            return None
        finally:
            conn.close()

    def get_all_meta(self) -> Dict[str, str]:
        """Return every row in ``rag_meta`` as ``{meta_key: meta_value}``.

        The indexer's per-cycle prelude reads the entire table in one
        shot to partition keys into ``last_indexed_*`` vs ``hash_*``
        groups. Exposing the bulk read here avoids spamming
        ``get_last_indexed`` N times per cycle.
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT meta_key, meta_value FROM rag_meta")
            return {r["meta_key"]: r["meta_value"] for r in cursor.fetchall()}
        except sqlite3.Error as e:
            logger.error(
                f"Database error reading rag_meta: {e}", exc_info=True,
            )
            return {}
        finally:
            conn.close()

    # --- Search side ----------------------------------------------------

    def search_similar(
        self,
        query_embedding: List[float],
        *,
        limit: int = 5,
        source_type_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """kNN over ``rag_embeddings`` joined with ``rag_chunks``.

        Returns a list of chunk dicts (chunk_id, source_type, source_ref,
        chunk_text, indexed_at, metadata, distance) ordered by ascending
        distance. The sqlite-vec ``MATCH`` + ``k = ?`` syntax lives
        inside this method — callers describe what they want, never
        spell the SQL.

        SECURITY: this seam owns secret redaction. Chunks carrying a
        secret ``project_context`` row (``source_type == "context"`` with
        a secret ``source_ref``) are dropped here so no caller — current
        or future — echoes a ``config_*_token`` value to the LLM/worker.
        This is retrieval-time defense against a stale index that
        embedded the secret before the index-time skip existed.

        ``source_type_filter`` is applied as a Python-side filter
        AFTER the vec0 join. vec0's ``WHERE`` clause only supports its
        own ``MATCH``/``k`` predicates; a SQL ``AND source_type = ?``
        runs against the joined rag_chunks side, which is fine. We
        spell it that way so the index hint stays.

        Degraded paths:
        * sqlite-vec not loadable → returns ``[]``
        * ``rag_embeddings`` table absent → returns ``[]``
        """
        if not g.global_vss_load_successful:
            return []

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            if not _embeddings_table_exists(cursor):
                logger.warning(
                    "RAG search: 'rag_embeddings' table not found; "
                    "skipping vector search."
                )
                return []

            query_embedding_json = json.dumps(query_embedding)
            # vec0 expects MATCH on the embedding column with the JSON-
            # encoded vector; the ``k = ?`` bind is vec0's knn-limit
            # syntax, and ``ORDER BY distance`` finishes the contract.
            sql = (
                "SELECT c.chunk_id, c.source_type, c.source_ref, "
                "       c.chunk_text, c.indexed_at, c.metadata, "
                "       r.distance "
                "FROM rag_embeddings r "
                "JOIN rag_chunks c ON r.rowid = c.chunk_id "
                "WHERE r.embedding MATCH ? AND k = ? "
                "ORDER BY r.distance"
            )
            # When a source-type filter is requested we ask vec0 for
            # extra rows so the post-filter doesn't starve the limit.
            # Pulling 4x the requested limit covers the common case of
            # a few unrelated source types in the top-k without
            # changing the on-disk ordering.
            effective_k = limit if source_type_filter is None else limit * 4
            cursor.execute(sql, (query_embedding_json, effective_k))
            raw = cursor.fetchall()

            results: List[Dict[str, Any]] = []
            for row in raw:
                if (source_type_filter is not None and
                        row["source_type"] != source_type_filter):
                    continue
                d = _chunk_to_dict(row)
                d["distance"] = row["distance"]
                results.append(d)
                if len(results) >= limit:
                    break
            # SECURITY: the seam drops any chunk that carries a credential
            # — secret-keyed context rows AND chunks of any source type
            # whose TEXT embeds a secret — before they reach any caller.
            # Retrieval-time defense against a stale index that embedded a
            # secret before the bulk_index_chunks choke-point existed. See
            # _drop_secret_chunks.
            return _drop_secret_chunks(results)
        except sqlite3.Error as e:
            logger.error(
                f"Database error during vector search: {e}", exc_info=True,
            )
            return []
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"Unexpected error during vector search: {e}", exc_info=True,
            )
            return []
        finally:
            conn.close()

    def get_chunk_by_id(self, chunk_id: int) -> Optional[Dict[str, Any]]:
        """Single chunk fetch by ``chunk_id``. Returns ``None`` if absent.

        Used for follow-up hydration when a caller has a chunk id from
        a prior search and wants the full row without re-running the
        kNN.
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT chunk_id, source_type, source_ref, chunk_text, "
                "       indexed_at, metadata "
                "FROM rag_chunks WHERE chunk_id = ?",
                (chunk_id,),
            )
            row = cursor.fetchone()
            return _chunk_to_dict(row) if row is not None else None
        except sqlite3.Error as e:
            logger.error(
                f"Database error fetching chunk {chunk_id}: {e}",
                exc_info=True,
            )
            return None
        finally:
            conn.close()

    def fetch_recent_context(
        self,
        since: str,
        *,
        limit: Optional[int] = 5,
    ) -> List[Dict[str, Any]]:
        """Return ``project_context`` rows whose ``updated_at > since``.

        The query path uses this to surface recently-changed context
        entries alongside the kNN search results (the "live context"
        section in :func:`query_rag_system`). Returned in descending
        ``updated_at`` order, capped at ``limit``.

        ``limit=None`` removes the SQL ``LIMIT`` clause entirely
        (unbounded read) — used by :func:`query_rag_system_with_model`,
        which historically read the *whole* ``project_context`` table
        with no time window or row cap. Passing ``since="1970-01-01T
        00:00:00Z"`` together with ``limit=None`` reproduces that
        unbounded read through this seam instead of a bespoke query.

        Returns a list of dicts with keys ``context_key``, ``value``,
        ``description``, ``updated_at``.

        SECURITY: this seam owns secret redaction. Secret-keyed rows
        (``config_*_token`` etc.) and rows whose VALUE embeds a
        credential are dropped here — the RAG live-context section is
        readable by any worker via ``ask_project_rag``, bypassing
        ``view_project_context``'s redaction, so the same policy is
        enforced at the seam rather than re-applied by each caller.
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            if limit is None:
                cursor.execute(
                    "SELECT context_key, value, description, updated_at "
                    "FROM project_context "
                    "WHERE updated_at > ? "
                    "ORDER BY updated_at DESC",
                    (since,),
                )
            else:
                cursor.execute(
                    "SELECT context_key, value, description, updated_at "
                    "FROM project_context "
                    "WHERE updated_at > ? "
                    "ORDER BY updated_at DESC "
                    "LIMIT ?",
                    (since, limit),
                )
            rows = [dict(r) for r in cursor.fetchall()]
            # SECURITY: the seam never surfaces secret-keyed rows
            # (config_*_token etc.) or rows whose VALUE embeds a
            # credential. The RAG live-context section is readable by any
            # worker via ask_project_rag, bypassing view_project_context's
            # redaction — so the same policy is enforced here at the seam.
            return [
                r
                for r in rows
                if not _is_secret_key(r.get("context_key"))
                and not _value_has_embedded_secret(
                    r.get("value"), r.get("description")
                )
            ]
        except sqlite3.Error as e:
            logger.error(
                f"Database error fetching recent context: {e}",
                exc_info=True,
            )
            return []
        finally:
            conn.close()


__all__ = ["RagRepository"]
