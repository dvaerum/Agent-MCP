"""ADR-0017 (Wave 12 PR B): the RAG retrieval SEAM returns rows in FULL.

This suite pinned the retrieval seam (``RagRepository.search_similar`` /
``fetch_recent_context``) as the owner of secret redaction. Wave 12 PR B
removes content-based secret detection entirely — the seam returns
``project_context`` rows and chunks AS-IS. Protection is by authorization
(RAG is per-project and, for tasks, ownership-scoped), not by guessing
content. Real secrets belong in the operator-only, non-RAG
project_settings store.

The former "the seam drops the secret row/chunk" assertions are inverted
here to "the seam returns it in full", asserted directly at the repository
boundary.
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
        from agent_mcp.core.config import embedding_settings
        dim = embedding_settings().dimension
    else:
        m = re.search(r"FLOAT\[(\d+)\]", row[0])
        dim = int(m.group(1)) if m else 1536
    out = list(values) + [0.0] * max(0, dim - len(values))
    return out[:dim]


# --- fetch_recent_context: the seam returns rows in full ---------------


def test_fetch_recent_context_returns_secret_keyed_row(
    project_dir, reset_globals,
):
    """A direct ``fetch_recent_context`` call returns a secret-named
    project_context row AS-IS (ADR-0017)."""
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

        assert "config_aoe_bearer_token" in keys
        assert any(_SECRET_VALUE in str(v) for v in values)
        assert "project_readme" in keys


def test_fetch_recent_context_returns_embedded_secret_value(
    project_dir, reset_globals,
):
    """A non-secret KEY whose VALUE embeds a credential is returned AS-IS
    by the seam (ADR-0017 — no content scan)."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo
        from agent_mcp.db.connection import get_db_connection

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
        assert "deploy_notes" in keys
        assert "project_readme" in keys


# --- search_similar: the seam returns context chunks in full -----------


def test_search_similar_returns_secret_context_chunk(
    project_dir, reset_globals,
):
    """A ``source_type == 'context'`` chunk keyed on a secret-named ref is
    returned AS-IS by the search seam (ADR-0017)."""
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

        assert "config_aoe_bearer_token" in source_refs
        assert _SECRET_VALUE in texts
        assert "app/util.py" in source_refs
