"""Contract tests for the class-based ``RagRepository`` (PR F of round-2
architecture-review series — the final repository in the four-concept
set: tasks, agents, messages, RAG).

This file pins the contract: a class-based Repository on
``agent_mcp.repositories.rag_repo`` that is the **single owner** of
``rag_chunks``, ``rag_embeddings`` and ``rag_meta``. The sqlite-vec
vector-search dialect (vec0 virtual table, ``MATCH ?``, ``k = ?``
ordering by distance) lives inside this class — callers describe what
they want, not how the kNN is spelled.

Design (Dennis's call, Option 1): one repository for ingest + search.
The methods cluster in two groups but share a table — splitting along
ingest/search would split the invariants.

What this test file pins:

* The singleton exists at ``agent_mcp.repositories.rag_repo`` after
  application lifespan startup, and points at a ``RagRepository``
  instance.
* ``bulk_index_chunks`` inserts N chunks + their embeddings atomically
  and returns the count. Re-indexing the same ``(source_type,
  source_ref)`` first deletes the old rows (the
  ``delete_chunks_for`` seam).
* ``set_meta`` writes ``last_indexed_*`` and ``hash_*`` rows.
* ``get_last_indexed`` returns the most recent timestamp for a
  source type.
* ``search_similar`` performs kNN over ``rag_embeddings`` and hydrates
  the resulting chunks; respects ``limit`` and
  ``source_type_filter``.
* ``get_chunk_by_id`` returns a single hydrated chunk.
* ``fetch_recent_context`` returns time-windowed reads.
* The connection-passing seam: ``bulk_index_chunks(connection=cur)``
  joins a parent transaction.

These tests fail on ``main`` because:

* ``agent_mcp.repositories.rag_repository`` (the class module) does
  not yet exist.
* ``agent_mcp.repositories.rag_repo`` (the lifespan singleton) is not
  exposed from the top-level repositories package.

A note on the sqlite-vec gate: the indexer in ``features/rag`` already
skips its whole cycle when ``is_vss_loadable()`` is False. The repo
mirrors that — search returns ``[]`` and bulk-index degrades to a
chunks-only insert with no companion embedding row. The CI environment
has sqlite-vec installed (we just verified ``import sqlite_vec``
succeeds in the worktree); the tests assume it loads. Where it
doesn't, we skip with a clear reason rather than producing brittle
green-because-degraded results.
"""

from __future__ import annotations

import datetime
import json

import pytest
from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app


def _make_client(project_dir):
    """Spin up the in-process app + TestClient so the lifespan hook runs.

    Same shape as ``test_message_repository._make_client``: a fresh
    client per test means every call exercises the lifespan startup
    that wires the singleton.
    """
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _vss_available() -> bool:
    """True when sqlite-vec loaded in the prior schema bootstrap.

    The lifespan startup calls ``check_vss_loadability()`` which sets
    ``g.global_vss_load_successful``. Tests that need the virtual
    table consult this so they degrade to ``pytest.skip`` on hosts
    without the extension rather than producing brittle red.
    """
    from agent_mcp.core import globals as g
    return bool(g.global_vss_load_successful)


# Use a vector dimension matching the on-disk vec0 table. The schema
# bootstrap reads EMBEDDING_DIMENSION at virtual-table-create time, and
# a leaky-fixture interaction with test_embedding_config can leave the
# config module's EMBEDDING_DIMENSION out of sync with the dim baked
# into the table for this test's project_dir. Re-reading the table's
# declared dimension straight from sqlite_master keeps the fixtures
# correct regardless of import-time state.
def _emb(values):
    """Pad/truncate a short fixture vector to whatever dimension the
    on-disk vec0 ``rag_embeddings`` table declares."""
    import re
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type IN ('table', 'virtual') AND name='rag_embeddings'"
        )
        row = c.fetchone()
    finally:
        conn.close()
    if row is None:
        # No table: fall back to the config value (the test that needs
        # the table will skip via _vss_available).
        from agent_mcp.core.config import EMBEDDING_DIMENSION
        dim = EMBEDDING_DIMENSION
    else:
        m = re.search(r"FLOAT\[(\d+)\]", row[0])
        dim = int(m.group(1)) if m else 1536
    out = list(values) + [0.0] * max(0, dim - len(values))
    return out[:dim]


# --- Singleton + lifespan wiring ---------------------------------------


def test_rag_repo_singleton_is_ragrepository_instance(
    project_dir, reset_globals,
):
    """``agent_mcp.repositories.rag_repo`` resolves to a class instance.

    The plan locks "module singletons, lifespan-owned" — same shape as
    the other three repos. The attribute access is
    ``from agent_mcp.repositories import rag_repo`` and the value is
    an instance, not a module.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.repositories.rag_repository import RagRepository

        assert isinstance(rag_repo, RagRepository), (
            "rag_repo must be a RagRepository instance after lifespan "
            "startup so call sites can rely on the class-based contract"
        )


# --- Ingest side -------------------------------------------------------


def test_bulk_index_chunks_inserts_chunks_and_embeddings(
    project_dir, reset_globals,
):
    """``bulk_index_chunks`` inserts N chunk rows + N embedding rows.

    Returns the count of chunks written. Each chunk dict carries the
    chunk text, the embedding vector, and optional metadata.
    """
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        chunks = [
            {
                "chunk_text": "hello world",
                "embedding": _emb([1.0, 0.0, 0.0]),
                "metadata": {"section": "intro"},
            },
            {
                "chunk_text": "goodbye world",
                "embedding": _emb([0.0, 1.0, 0.0]),
                "metadata": {"section": "outro"},
            },
        ]
        n = rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/example.md",
            chunks=chunks,
        )
        assert n == 2

        # Verify directly that two chunks and two embeddings landed.
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT COUNT(*) FROM rag_chunks WHERE source_type = ? "
                "AND source_ref = ?",
                ("markdown", "docs/example.md"),
            )
            assert c.fetchone()[0] == 2
            c.execute("SELECT COUNT(*) FROM rag_embeddings")
            assert c.fetchone()[0] >= 2
        finally:
            conn.close()


def test_delete_chunks_for_removes_chunks_and_embeddings(
    project_dir, reset_globals,
):
    """``delete_chunks_for`` removes both the chunk row and its embedding.

    The vec0 virtual table is linked to ``rag_chunks`` by ``rowid``;
    the repo must delete from both tables so a re-index doesn't leave
    orphan vectors.
    """
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        # Seed one chunk first.
        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/a.md",
            chunks=[
                {"chunk_text": "x", "embedding": _emb([1.0]), "metadata": None}
            ],
        )
        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/b.md",
            chunks=[
                {"chunk_text": "y", "embedding": _emb([2.0]), "metadata": None}
            ],
        )

        n_deleted = rag_repo.delete_chunks_for("markdown", "docs/a.md")
        assert n_deleted == 1

        conn = get_db_connection()
        try:
            c = conn.cursor()
            # docs/a.md gone.
            c.execute(
                "SELECT COUNT(*) FROM rag_chunks "
                "WHERE source_type = ? AND source_ref = ?",
                ("markdown", "docs/a.md"),
            )
            assert c.fetchone()[0] == 0
            # docs/b.md still there.
            c.execute(
                "SELECT COUNT(*) FROM rag_chunks "
                "WHERE source_type = ? AND source_ref = ?",
                ("markdown", "docs/b.md"),
            )
            assert c.fetchone()[0] == 1
        finally:
            conn.close()


def test_set_and_get_last_indexed(project_dir, reset_globals):
    """``set_meta`` writes ``last_indexed_<source>`` and ``get_last_indexed``
    reads it back as an ISO string. Idempotent on rewrite."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo

        ts = "2026-06-11T12:00:00Z"
        rag_repo.set_meta(source_type="markdown", last_indexed_at=ts)
        assert rag_repo.get_last_indexed("markdown") == ts

        ts2 = "2026-06-12T08:00:00Z"
        rag_repo.set_meta(source_type="markdown", last_indexed_at=ts2)
        assert rag_repo.get_last_indexed("markdown") == ts2


def test_set_meta_writes_source_hashes(project_dir, reset_globals):
    """``set_meta`` also takes per-source content hashes.

    The indexer's loop today writes ``hash_<source_type>_<source_ref>``
    keys alongside the ``last_indexed_*`` keys to skip unchanged
    files; the repo exposes the same surface so the migration is a
    straight call swap.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        rag_repo.set_meta(
            source_type="markdown",
            source_hashes={"docs/a.md": "abc123", "docs/b.md": "def456"},
        )

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT meta_value FROM rag_meta WHERE meta_key = ?",
                ("hash_markdown_docs/a.md",),
            )
            assert c.fetchone()["meta_value"] == "abc123"
            c.execute(
                "SELECT meta_value FROM rag_meta WHERE meta_key = ?",
                ("hash_markdown_docs/b.md",),
            )
            assert c.fetchone()["meta_value"] == "def456"
        finally:
            conn.close()


def test_get_last_indexed_returns_epoch_baseline_when_unset(
    project_dir, reset_globals,
):
    """``get_last_indexed`` returns the seeded epoch baseline for a fresh DB.

    The schema bootstrap seeds the canonical ``1970-01-01T00:00:00Z``
    for every known source type (see
    ``_DEFAULT_RAG_META_ENTRIES`` in ``db/schema.py``). The indexer
    relies on that to treat a fresh DB as "everything needs re-
    indexing".
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo

        assert rag_repo.get_last_indexed("markdown") == "1970-01-01T00:00:00Z"
        assert rag_repo.get_last_indexed("code") == "1970-01-01T00:00:00Z"
        # An unknown source type → None (no row).
        assert rag_repo.get_last_indexed("totally-new-source-type") is None


# --- Search side ------------------------------------------------------


def test_search_similar_ranks_by_cosine_distance(
    project_dir, reset_globals,
):
    """``search_similar`` returns the closest chunks first, ranked by
    sqlite-vec's distance metric.

    Three orthogonal vectors are indexed; querying with the exact
    first vector ranks chunk_1 first (distance=0).
    """
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        # Three chunks with orthogonal unit vectors so the kNN ranking
        # is deterministic regardless of distance metric.
        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/rank.md",
            chunks=[
                {
                    "chunk_text": "alpha",
                    "embedding": _emb([1.0, 0.0, 0.0]),
                    "metadata": None,
                },
                {
                    "chunk_text": "beta",
                    "embedding": _emb([0.0, 1.0, 0.0]),
                    "metadata": None,
                },
                {
                    "chunk_text": "gamma",
                    "embedding": _emb([0.0, 0.0, 1.0]),
                    "metadata": None,
                },
            ],
        )

        # Query: exactly the first vector. Closest match is "alpha".
        results = rag_repo.search_similar(
            query_embedding=_emb([1.0, 0.0, 0.0]),
            limit=3,
        )
        assert len(results) == 3
        assert results[0]["chunk_text"] == "alpha"


def test_search_similar_respects_limit(project_dir, reset_globals):
    """The ``limit=`` keyword caps the number of returned chunks."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/limit.md",
            chunks=[
                {"chunk_text": f"chunk-{i}",
                 "embedding": _emb([float(i + 1)]),
                 "metadata": None}
                for i in range(5)
            ],
        )

        results = rag_repo.search_similar(
            query_embedding=_emb([1.0]),
            limit=2,
        )
        assert len(results) == 2


def test_search_similar_respects_source_type_filter(
    project_dir, reset_globals,
):
    """The ``source_type_filter=`` keyword scopes results to one source.

    Indexes a markdown chunk and a code chunk; filtering to ``code``
    returns only the code chunk.
    """
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/m.md",
            chunks=[
                {"chunk_text": "from-md",
                 "embedding": _emb([1.0]),
                 "metadata": None}
            ],
        )
        rag_repo.bulk_index_chunks(
            source_type="code",
            source_ref="src/a.py",
            chunks=[
                {"chunk_text": "from-code",
                 "embedding": _emb([1.0]),
                 "metadata": None}
            ],
        )

        results = rag_repo.search_similar(
            query_embedding=_emb([1.0]),
            limit=10,
            source_type_filter="code",
        )
        assert len(results) == 1
        assert results[0]["chunk_text"] == "from-code"
        assert results[0]["source_type"] == "code"


def test_search_similar_returns_empty_when_table_absent(
    project_dir, reset_globals, monkeypatch,
):
    """When the vec0 table can't be queried (no extension), search returns
    an empty list and does NOT raise.

    Simulates the degraded path by setting ``g.global_vss_load_successful``
    False — the same condition the indexer's ``is_vss_loadable()`` check
    consults today.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.core import globals as g

        monkeypatch.setattr(g, "global_vss_load_successful", False)
        results = rag_repo.search_similar(
            query_embedding=_emb([1.0]),
            limit=10,
        )
        assert results == []


def test_search_similar_hydrates_metadata(project_dir, reset_globals):
    """The result dicts carry parsed metadata (JSON → dict)."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        rag_repo.bulk_index_chunks(
            source_type="code",
            source_ref="src/x.py",
            chunks=[
                {
                    "chunk_text": "def hello(): pass",
                    "embedding": _emb([1.0]),
                    "metadata": {"language": "python", "section": "function"},
                },
            ],
        )

        results = rag_repo.search_similar(
            query_embedding=_emb([1.0]),
            limit=1,
        )
        assert len(results) == 1
        assert results[0]["metadata"] == {
            "language": "python", "section": "function",
        }


def test_get_chunk_by_id_returns_hydrated_chunk(
    project_dir, reset_globals,
):
    """``get_chunk_by_id`` returns a single chunk dict, or None if missing."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/by-id.md",
            chunks=[
                {"chunk_text": "the unique chunk",
                 "embedding": _emb([1.0]),
                 "metadata": None}
            ],
        )

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT chunk_id FROM rag_chunks "
                "WHERE source_ref = ?",
                ("docs/by-id.md",),
            )
            chunk_id = c.fetchone()["chunk_id"]
        finally:
            conn.close()

        chunk = rag_repo.get_chunk_by_id(chunk_id)
        assert chunk is not None
        assert chunk["chunk_text"] == "the unique chunk"
        assert chunk["source_type"] == "markdown"
        assert chunk["source_ref"] == "docs/by-id.md"

        assert rag_repo.get_chunk_by_id(999_999) is None


def test_fetch_recent_context_time_windowed(project_dir, reset_globals):
    """``fetch_recent_context`` returns rows from ``project_context`` whose
    ``updated_at > since``. This is the same time-window the indexer
    uses to decide what context entries need re-embedding.

    Distinct from kNN search; this is a SQL-only read that the query
    path uses to surface "recently changed" context entries before
    falling back to vector search.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        # Seed two project_context rows with distinct timestamps far
        # in the future so the admin-token row (inserted at lifespan
        # startup with a "now" timestamp) doesn't pollute the
        # comparison. The cutoff sits between the two seeded rows.
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO project_context "
                "(context_key, value, description, created_at, "
                "created_by, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("old_key", '"old"', "old desc",
                 "2099-06-01T00:00:00Z", "test",
                 "2099-06-01T00:00:00Z", "test"),
            )
            c.execute(
                "INSERT INTO project_context "
                "(context_key, value, description, created_at, "
                "created_by, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("new_key", '"new"', "new desc",
                 "2099-06-10T00:00:00Z", "test",
                 "2099-06-10T00:00:00Z", "test"),
            )
            conn.commit()
        finally:
            conn.close()

        results = rag_repo.fetch_recent_context(since="2099-06-05T00:00:00Z")
        assert len(results) == 1
        assert results[0]["context_key"] == "new_key"


def test_bulk_index_chunks_joins_parent_transaction(
    project_dir, reset_globals,
):
    """When ``connection=`` is passed, the inserts happen on that cursor
    and stay invisible until the caller commits.

    This is the same transaction-passing seam the other three repos
    expose. The indexer today holds its own ``conn`` for an entire
    cycle; after migration it can pass that cursor into the repo and
    get a single atomic commit at end-of-cycle.
    """
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        owner = get_db_connection()
        try:
            cursor = owner.cursor()
            n = rag_repo.bulk_index_chunks(
                source_type="markdown",
                source_ref="docs/tx.md",
                chunks=[
                    {"chunk_text": "tx-chunk",
                     "embedding": _emb([1.0]),
                     "metadata": None}
                ],
                connection=cursor,
            )
            assert n == 1

            # Verify on a *separate* connection that the row is NOT
            # visible (parent transaction still open).
            observer = get_db_connection()
            try:
                c2 = observer.cursor()
                c2.execute(
                    "SELECT COUNT(*) FROM rag_chunks WHERE source_ref = ?",
                    ("docs/tx.md",),
                )
                # In WAL mode another connection sees committed rows
                # only. Since we haven't committed, the count must be 0.
                assert c2.fetchone()[0] == 0
            finally:
                observer.close()

            # Now commit and verify the row is visible.
            owner.commit()
            observer = get_db_connection()
            try:
                c2 = observer.cursor()
                c2.execute(
                    "SELECT COUNT(*) FROM rag_chunks WHERE source_ref = ?",
                    ("docs/tx.md",),
                )
                assert c2.fetchone()[0] == 1
            finally:
                observer.close()
        finally:
            owner.close()


def test_bulk_index_replaces_after_delete(project_dir, reset_globals):
    """Re-indexing flow: caller calls ``delete_chunks_for`` then
    ``bulk_index_chunks`` for the same source — leaves the table with
    only the new chunks. This is the indexer's per-file re-index
    pattern."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/replace.md",
            chunks=[
                {"chunk_text": f"old-{i}",
                 "embedding": _emb([float(i + 1)]),
                 "metadata": None}
                for i in range(3)
            ],
        )

        rag_repo.delete_chunks_for("markdown", "docs/replace.md")
        rag_repo.bulk_index_chunks(
            source_type="markdown",
            source_ref="docs/replace.md",
            chunks=[
                {"chunk_text": "new-only",
                 "embedding": _emb([1.0]),
                 "metadata": None}
            ],
        )

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT chunk_text FROM rag_chunks WHERE source_ref = ?",
                ("docs/replace.md",),
            )
            rows = [r["chunk_text"] for r in c.fetchall()]
            assert rows == ["new-only"]
        finally:
            conn.close()
