"""Security R2-F3 — RAG secret redaction must cover ALL ingested chunk
source-types, not just ``source_type == 'context'``.

FINDING (owner-authorized pentest, HIGH, live-reproduced): any agent with
the ``rag.query`` capability (every registered worker/manager) could
exfiltrate a credential embedded in a **task description/title, code
file, or markdown doc** via ``ask_project_rag``. The round-2
secret-redaction fix only protected ``project_context`` rows: the
index-time value scan, the retrieval-seam drop, and the query-path
defense-in-depth were all keyed on ``source_type == 'context'``. Other
source types (task/code/markdown/code_summary) were embedded verbatim
and echoed back by the LLM.

Live repro: a task whose description contained
``ghp_R2LeakHunt0000abcdefGHIJKLMNOPQRST99`` was auto-indexed on create;
a worker then asked ``ask_project_rag`` "what is the GitHub deploy
token?" and the token came back verbatim.

Fix (single choke-point + retrieval-seam defense-in-depth + one-time
purge):

  1. ``RagRepository.bulk_index_chunks`` scans every chunk's TEXT with
     ``_value_has_embedded_secret`` for ALL source types and skips a
     secret-bearing chunk. This one seam covers the periodic indexer,
     ``index_task_data`` and any future ingester.
  2. ``_drop_secret_chunks`` (renamed from ``_drop_secret_context_chunks``)
     drops any retrieved chunk whose ``chunk_text`` embeds a credential
     — defense against a stale/pre-existing index.
  3. A one-time all-source purge evicts already-indexed secret-bearing
     chunks of any source type, guarded by its own ``rag_meta`` flag.

These tests were RED on the pre-fix tree (secret chunks were indexed,
retrievable, and the new symbols did not exist).
"""

from __future__ import annotations

import datetime

import pytest
from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app


# The exact token the pentester exfiltrated (matches the ``gh[pousr]_``
# well-known-shape pattern in the embedded-secret scanner).
_GH_SECRET = "ghp_R2LeakHunt0000abcdefGHIJKLMNOPQRST99"


def _make_client(project_dir):
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _vss_available() -> bool:
    from agent_mcp.core import globals as g

    return bool(g.global_vss_load_successful)


def _emb(values):
    """Pad/truncate a short fixture vector to the on-disk vec0 dimension.

    Copied from ``test_rag_repository`` — keeps the fixture correct
    regardless of import-time embedding-dimension state.
    """
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


def _chunk_texts(source_ref: str):
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT chunk_text FROM rag_chunks WHERE source_ref = ?",
            (source_ref,),
        )
        return [r["chunk_text"] for r in c.fetchall()]
    finally:
        conn.close()


def _seed_rag_chunk(source_type: str, source_ref: str, text: str) -> None:
    """Insert a rag_chunks row directly — bypasses the bulk_index
    choke-point so we can prove the retrieval-seam + purge defenses on
    an already-poisoned index. Skips rag_embeddings on purpose (vec0 may
    be absent; the purge guards that separately)."""
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO rag_chunks (source_type, source_ref, chunk_text, "
            "indexed_at, metadata) VALUES (?, ?, ?, ?, ?)",
            (source_type, source_ref, text, now, None),
        )
        conn.commit()
    finally:
        conn.close()


# ── (1) ingest choke-point — bulk_index_chunks skips secret chunks ───


@pytest.mark.parametrize("source_type", ["task", "code", "markdown"])
def test_bulk_index_skips_secret_bearing_chunk(
    project_dir, reset_globals, source_type
):
    """A chunk of ANY source type whose text embeds a credential is
    never written to ``rag_chunks`` (so it can never reach the LLM)."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo

        ref = f"src/{source_type}-leak"
        secret_text = (
            f"The GitHub deploy token is {_GH_SECRET}; use it for CI."
        )
        n = rag_repo.bulk_index_chunks(
            source_type=source_type,
            source_ref=ref,
            chunks=[
                {
                    "chunk_text": secret_text,
                    "embedding": _emb([1.0]),
                    "metadata": None,
                }
            ],
        )
        assert n == 0, "secret-bearing chunk must be skipped, not counted"
        assert _chunk_texts(ref) == [], (
            f"{source_type} chunk carrying {_GH_SECRET} was indexed"
        )


def test_bulk_index_skips_secret_but_keeps_benign_in_same_batch(
    project_dir, reset_globals
):
    """Mixed batch: only the secret-bearing chunk is dropped; the benign
    sibling is still indexed (per-chunk, not per-batch, redaction)."""
    with _make_client(project_dir):
        from agent_mcp.repositories import rag_repo

        benign = "This task implements the login form validation."
        n = rag_repo.bulk_index_chunks(
            source_type="task",
            source_ref="task-mixed",
            chunks=[
                {
                    "chunk_text": benign,
                    "embedding": _emb([1.0]),
                    "metadata": None,
                },
                {
                    "chunk_text": f"deploy token {_GH_SECRET}",
                    "embedding": _emb([2.0]),
                    "metadata": None,
                },
            ],
        )
        assert n == 1
        assert _chunk_texts("task-mixed") == [benign]


# ── regression — benign non-context chunk stays retrievable ──────────


def test_benign_task_chunk_still_indexed_and_retrievable(
    project_dir, reset_globals
):
    """Don't over-redact: a normal task chunk with no secret is indexed
    and comes back through ``search_similar``."""
    with _make_client(project_dir):
        if not _vss_available():
            pytest.skip("sqlite-vec not loadable on this host")
        from agent_mcp.repositories import rag_repo

        benign = (
            "Refactor the authentication middleware to use dependency "
            "injection so it can be unit tested."
        )
        n = rag_repo.bulk_index_chunks(
            source_type="task",
            source_ref="task-benign",
            chunks=[
                {
                    "chunk_text": benign,
                    "embedding": _emb([1.0]),
                    "metadata": None,
                }
            ],
        )
        assert n == 1
        results = rag_repo.search_similar(
            query_embedding=_emb([1.0]), limit=5
        )
        assert any(benign in r["chunk_text"] for r in results), (
            "benign task chunk was over-redacted / not retrievable"
        )


# ── (2) retrieval seam — _drop_secret_chunks drops non-context leaks ──


def test_repo_drop_secret_chunks_filters_noncontext_secret():
    """The repo's retrieval-seam filter drops a non-context chunk whose
    TEXT embeds a credential, and keeps benign chunks (stale-index
    defense; needs no app/DB — pure filter over dicts)."""
    from agent_mcp.repositories.rag_repository import _drop_secret_chunks

    results = [
        {
            "source_type": "task",
            "source_ref": "task-9",
            "chunk_text": f"the token is {_GH_SECRET}",
        },
        {
            "source_type": "code",
            "source_ref": "app/util.py",
            "chunk_text": "def hello(): return 'world'",
        },
        {
            # existing context-KEY drop must still fire even if the text
            # itself has no token shape.
            "source_type": "context",
            "source_ref": "config_aoe_bearer_token",
            "chunk_text": "Context Key: config_aoe_bearer_token",
        },
    ]
    kept = _drop_secret_chunks(results)
    assert all(_GH_SECRET not in r["chunk_text"] for r in kept)
    assert not any(r["source_type"] == "context" for r in kept), (
        "secret-keyed context chunk must still be dropped"
    )
    assert [r["source_ref"] for r in kept] == ["app/util.py"]


def test_query_drop_secret_chunks_filters_noncontext_secret():
    """The query-path defense-in-depth copy mirrors the repo seam for
    callers that inject/mock the repo."""
    from agent_mcp.features.rag.query import _drop_secret_chunks

    results = [
        {
            "source_type": "markdown",
            "source_ref": "README.md",
            "chunk_text": f"export TOKEN={_GH_SECRET}",
        },
        {
            "source_type": "task",
            "source_ref": "task-1",
            "chunk_text": "implement the search box",
        },
    ]
    kept = _drop_secret_chunks(results)
    assert all(_GH_SECRET not in r["chunk_text"] for r in kept)
    assert [r["source_ref"] for r in kept] == ["task-1"]


# ── (3) one-time purge — evict already-indexed non-context secrets ───


def test_all_source_purge_evicts_noncontext_secret_chunks_once(
    project_dir, reset_globals
):
    """Directly-seeded (pre-fix) secret chunks of any source type are
    evicted by the one-time purge; benign chunks survive; a second run
    is a guarded no-op."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.features.rag.indexing import (
            _run_all_source_secret_purge,
        )
        from agent_mcp.repositories import rag_repo

        _seed_rag_chunk("task", "task-leak", f"token: {_GH_SECRET}")
        _seed_rag_chunk("markdown", "docs/leak.md", f"key {_GH_SECRET} end")
        _seed_rag_chunk("code", "app/util.py", "def hello(): return 'world'")

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            purged = _run_all_source_secret_purge(
                cur, rag_repo.get_all_meta()
            )
            conn.commit()
        finally:
            conn.close()

        assert purged == 2, "both secret chunks (task + markdown) purged"
        assert _chunk_texts("task-leak") == []
        assert _chunk_texts("docs/leak.md") == []
        assert _chunk_texts("app/util.py") == ["def hello(): return 'world'"]

        # Second run: guarded by the rag_meta flag → skipped (-1).
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            purged2 = _run_all_source_secret_purge(
                cur, rag_repo.get_all_meta()
            )
            conn.commit()
        finally:
            conn.close()
        assert purged2 == -1, "purge must run exactly once (meta-key guard)"
