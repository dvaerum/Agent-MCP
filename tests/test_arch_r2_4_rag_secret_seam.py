"""Arch round-2 #4: the RAG retrieval SEAM owns secret redaction.

FINDING (latent): ``RagRepository.search_similar`` and
``RagRepository.fetch_recent_context`` are the single retrieval seam
for the RAG query path, but they returned RAW rows — including
secret-keyed ``project_context`` rows and chunks embedded from a stale
index. Redaction was re-applied BY HAND at every ``features/rag/query.py``
call site (``_drop_secret_context_chunks`` / ``_is_secret_key`` /
``_value_has_embedded_secret``). Any NEW caller of the seam would leak
secrets to the LLM/worker.

Fix: move the redaction INSIDE the two retrieval methods so the seam
that owns "what you get back" also owns "with secrets removed." These
tests assert the redaction AT THE REPOSITORY BOUNDARY — a direct call
to ``rag_repo.search_similar`` / ``rag_repo.fetch_recent_context``
returns no secret, independent of the query layer.

RED on ``main`` (the repo returns secrets raw); GREEN after the seam
enforces the filter. The pre-existing query-layer secret tests
(``test_sec_rag_secret_redaction.py``) stay green regardless.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app


_SECRET_VALUE = "SENTINEL-SECRET-VALUE-9f3a"
_PUBLIC_VALUE = "public-readme-info"


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _vss_available() -> bool:
    from agent_mcp.core import globals as g
    return bool(g.global_vss_load_successful)


def _emb(values):
    """Pad/truncate a fixture vector to the on-disk vec0 dimension."""
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
        from agent_mcp.core.config import EMBEDDING_DIMENSION
        dim = EMBEDDING_DIMENSION
    else:
        m = re.search(r"FLOAT\[(\d+)\]", row[0])
        dim = int(m.group(1)) if m else 1536
    out = list(values) + [0.0] * max(0, dim - len(values))
    return out[:dim]


# --- fetch_recent_context: the seam drops secret rows ------------------


def test_fetch_recent_context_drops_secret_keyed_row(
    project_dir, reset_globals,
):
    """A direct ``fetch_recent_context`` call must NOT return a
    secret-keyed (``config_*_token``) project_context row."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        conn = get_db_connection()
        try:
            c = conn.cursor()
            for key, value in (
                ("config_aoe_bearer_token", _SECRET_VALUE),
                ("project_readme", _PUBLIC_VALUE),
            ):
                c.execute(
                    "INSERT INTO project_context "
                    "(context_key, value, description, created_at, "
                    "created_by, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, f'"{value}"', "desc",
                     "2099-06-10T00:00:00Z", "test",
                     "2099-06-10T00:00:00Z", "test"),
                )
            conn.commit()
        finally:
            conn.close()

        results = rag_repo.fetch_recent_context(
            since="2099-06-01T00:00:00Z", limit=10,
        )
        keys = [r["context_key"] for r in results]
        values = [r.get("value") for r in results]

        assert "config_aoe_bearer_token" not in keys, (
            "secret-keyed row leaked out of the fetch_recent_context seam"
        )
        assert all(_SECRET_VALUE not in str(v) for v in values), (
            "secret VALUE leaked out of the fetch_recent_context seam"
        )
        # Public row survives (no over-filtering).
        assert "project_readme" in keys


def test_fetch_recent_context_drops_embedded_secret_value(
    project_dir, reset_globals,
):
    """A non-secret KEY whose VALUE embeds a credential must also be
    dropped by the seam (belt-and-suspenders, same as the index/query
    embedded-secret scan)."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

        # A recognisable embedded credential (GitHub PAT prefix).
        embedded = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        conn = get_db_connection()
        try:
            c = conn.cursor()
            for key, value in (
                ("deploy_notes", embedded),
                ("project_readme", _PUBLIC_VALUE),
            ):
                c.execute(
                    "INSERT INTO project_context "
                    "(context_key, value, description, created_at, "
                    "created_by, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, value, "desc",
                     "2099-06-10T00:00:00Z", "test",
                     "2099-06-10T00:00:00Z", "test"),
                )
            conn.commit()
        finally:
            conn.close()

        results = rag_repo.fetch_recent_context(
            since="2099-06-01T00:00:00Z", limit=10,
        )
        keys = [r["context_key"] for r in results]
        assert "deploy_notes" not in keys, (
            "row with an embedded credential VALUE leaked out of the seam"
        )
        assert "project_readme" in keys


# --- search_similar: the seam drops secret context chunks --------------


def test_search_similar_drops_secret_context_chunk(
    project_dir, reset_globals,
):
    """A ``source_type == 'context'`` chunk whose ``source_ref`` is a
    secret key (embedded into the index before the index-time skip
    existed) must be dropped by the search seam. Non-secret chunks
    survive."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        rag_repo.bulk_index_chunks(
            source_type="context",
            source_ref="config_aoe_bearer_token",
            chunks=[
                {
                    "chunk_text": (
                        "Context Key: config_aoe_bearer_token\n"
                        f"Value: {_SECRET_VALUE}"
                    ),
                    "embedding": _emb([1.0, 0.0, 0.0]),
                    "metadata": None,
                },
            ],
        )
        rag_repo.bulk_index_chunks(
            source_type="code",
            source_ref="app/util.py",
            chunks=[
                {
                    "chunk_text": "def hello(): return 'world'",
                    "embedding": _emb([1.0, 0.0, 0.0]),
                    "metadata": None,
                },
            ],
        )

        results = rag_repo.search_similar(
            query_embedding=_emb([1.0, 0.0, 0.0]),
            limit=10,
        )
        source_refs = [r["source_ref"] for r in results]
        texts = " ".join(r.get("chunk_text", "") for r in results)

        assert "config_aoe_bearer_token" not in source_refs, (
            "secret context chunk leaked out of the search_similar seam"
        )
        assert _SECRET_VALUE not in texts, (
            "secret VALUE leaked out of the search_similar seam"
        )
        # Non-secret code chunk survives.
        assert "app/util.py" in source_refs
