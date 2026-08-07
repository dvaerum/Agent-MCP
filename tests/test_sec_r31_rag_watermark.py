"""Data-integrity: the RAG incremental watermark must never advance past
a row that failed to embed this cycle (BL-R31-1).

FINDING (owner-authorized review, 2026-07, MED, data-integrity):
``run_rag_indexing_periodically`` selects rows to (re-)index with
``updated_at > last_indexed_<source>`` (the "watermark"). When an
embedding batch *partially* fails (a sub-50% transient error), the cycle
is still treated as successful, so:

  1. the failed row's chunks are skipped (never embedded), AND
  2. the watermark still advances to the max ``updated_at`` of every
     *scanned* row — including the un-embedded one.

Because the next cycle only re-scans ``updated_at > watermark``, the
un-embedded row is never re-selected, never reaches the hash-retry path,
and ``ask_project_rag`` serves it stale/missing FOREVER (until the row is
edited again and its ``updated_at`` bumps).

Fix: cap every watermark strictly below the earliest row that failed to
embed this cycle, and only advance a source's stored hash when the whole
source embedded. Failed rows are then re-scanned next cycle; clean rows
still advance.

These tests drive the real ``run_rag_indexing_periodically`` loop for a
bounded number of cycles with an injected embedder that fails one
specific context row, then assert on the ``rag_meta`` watermark and on
which rows the embedder was asked to embed each cycle.
"""

from __future__ import annotations

import anyio
import pytest

from agent_mcp.features.rag import indexing as indexing_mod
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_GOOD1_KEY = "ctxr31_good1_alpha"
_POISON_KEY = "ctxr31_poison_zeta"
_GOOD2_KEY = "ctxr31_good2_omega"

_GOOD1_TS = "2099-01-01T00:00:00Z"
_POISON_TS = "2099-02-01T00:00:00Z"
_GOOD2_TS = "2099-03-01T00:00:00Z"


def _seed_context_row(key: str, value: str, updated_at: str) -> None:
    """Insert a project_context row with an explicit ``updated_at`` so
    the indexer's watermark scan sees a deterministic ordering."""
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO project_context "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, f'"{value}"', "desc", updated_at, "test",
             updated_at, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _install_indexer_harness(
    monkeypatch,
    *,
    max_cycles: int,
    fail_cycles: set,
):
    """Wire the periodic indexer for a bounded, network-free run.

    Returns the shared ``state`` dict; ``state["embedded"][cycle]`` is
    the list of chunk texts the embedder was asked to embed in that
    cycle. The injected embedder leaves the poison row's chunk un-embedded
    (``None``) only for the cycles listed in ``fail_cycles``.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.repositories import rag_repo

    state = {"cycle": 0, "embedded": [], "max_cycles": max_cycles}

    async def fake_embed(batch_chunks, batch_index_start, results_list):
        cyc = state["cycle"]
        while len(state["embedded"]) <= cyc:
            state["embedded"].append([])
        for i, chunk in enumerate(batch_chunks):
            pos = batch_index_start + i
            if pos >= len(results_list):
                continue
            state["embedded"][cyc].append(chunk)
            if _POISON_KEY in chunk and cyc in fail_cycles:
                results_list[pos] = None  # simulate a transient failure
            else:
                results_list[pos] = [0.0, 0.0, 0.0, 0.0]
        return True

    async def fake_sleep(duration):
        # The only sleep >= 30s is the end-of-cycle boundary; the tiny
        # inter-batch-group sleeps (0.1s) are ignored so cycle counting
        # stays exact.
        if duration >= 30:
            state["cycle"] += 1
            if state["cycle"] >= state["max_cycles"]:
                g.server_running = False

    def fake_delete(source_type, source_ref, connection=None):
        return 0

    def fake_bulk(*, source_type, source_ref, chunks, connection=None):
        return len(chunks)

    # arch-r4 #2: the indexer no longer gates on OPENAI_API_KEY being
    # truthy — it always resolves a provider via embedding_client(),
    # so no OPENAI_API_KEY_ENV monkeypatch is needed here anymore.
    monkeypatch.setattr(indexing_mod, "is_vss_loadable", lambda: True)
    monkeypatch.setattr(
        indexing_mod, "_get_embeddings_batch", fake_embed
    )
    # Keep set_meta / get_all_meta REAL (they carry the watermark we
    # assert on); stub only the vec0-touching ingest calls so the test
    # is host-independent of sqlite-vec dimension quirks.
    monkeypatch.setattr(rag_repo, "delete_chunks_for", fake_delete)
    monkeypatch.setattr(rag_repo, "bulk_index_chunks", fake_bulk)
    monkeypatch.setattr(anyio, "sleep", fake_sleep)

    g.server_running = True
    g.startup_complete_event.set()
    return state


async def _run_indexer(state) -> None:
    await indexing_mod.run_rag_indexing_periodically(interval_seconds=300)


# ── Core finding: partial failure must not advance the watermark ──────


async def test_partial_embedding_failure_holds_context_watermark(
    tmp_path, monkeypatch
) -> None:
    """RED on unfixed code: with good1 < poison < good2 and only the
    poison row failing to embed, the context watermark must stay at
    good1 (below poison) so poison is re-scanned next cycle — NOT jump to
    good2 (the max scanned ``updated_at``), which would strand poison
    forever."""
    async with mcp_session(tmp_path):
        _seed_context_row(_GOOD1_KEY, "alpha-value", _GOOD1_TS)
        _seed_context_row(_POISON_KEY, "poison-value", _POISON_TS)
        _seed_context_row(_GOOD2_KEY, "omega-value", _GOOD2_TS)

        state = _install_indexer_harness(
            monkeypatch, max_cycles=1, fail_cycles={0}
        )
        await _run_indexer(state)

        # Guard against a vacuous pass: the cycle must actually have run
        # and scanned the poison row.
        assert any(
            _POISON_KEY in chunk for chunk in state["embedded"][0]
        ), "cycle did not run / poison row was never scanned"

        from agent_mcp.repositories import rag_repo

        watermark = rag_repo.get_last_indexed("context")
        assert watermark == _GOOD1_TS, (
            "context watermark advanced past the un-embedded poison row "
            f"(got {watermark!r}); poison would never be re-scanned"
        )
        assert watermark < _POISON_TS


async def test_failed_row_rescanned_and_embedded_next_cycle(
    tmp_path, monkeypatch
) -> None:
    """The previously-failed row must be re-selected next cycle and,
    when the transient error clears, embedded — while the healthy rows
    are NOT re-embedded (no infinite re-scan of clean rows)."""
    async with mcp_session(tmp_path):
        _seed_context_row(_GOOD1_KEY, "alpha-value", _GOOD1_TS)
        _seed_context_row(_POISON_KEY, "poison-value", _POISON_TS)
        _seed_context_row(_GOOD2_KEY, "omega-value", _GOOD2_TS)

        # Poison fails only in cycle 0; cycle 1 the transient clears.
        state = _install_indexer_harness(
            monkeypatch, max_cycles=2, fail_cycles={0}
        )
        await _run_indexer(state)

        assert len(state["embedded"]) == 2, "expected exactly two cycles"

        # Cycle 1 must re-scan poison (proves it wasn't stranded).
        cycle1 = state["embedded"][1]
        assert any(_POISON_KEY in c for c in cycle1), (
            "failed row was not re-scanned on the next cycle"
        )
        # ...and ONLY poison — the clean rows matched their stored hash
        # and must not be re-embedded.
        assert all(_POISON_KEY in c for c in cycle1), (
            "healthy rows were needlessly re-embedded next cycle: "
            f"{cycle1!r}"
        )

        from agent_mcp.repositories import rag_repo

        # With no failures left, the watermark advances to the newest row.
        watermark = rag_repo.get_last_indexed("context")
        assert watermark == _GOOD2_TS, (
            f"watermark did not advance after recovery (got {watermark!r})"
        )


async def test_clean_cycle_advances_watermark_fully(
    tmp_path, monkeypatch
) -> None:
    """Regression: a cycle with no embedding failures advances the
    context watermark to the newest scanned row (no over-holding)."""
    async with mcp_session(tmp_path):
        _seed_context_row(_GOOD1_KEY, "alpha-value", _GOOD1_TS)
        _seed_context_row(_GOOD2_KEY, "omega-value", _GOOD2_TS)

        state = _install_indexer_harness(
            monkeypatch, max_cycles=1, fail_cycles=set()
        )
        await _run_indexer(state)

        assert state["embedded"] and state["embedded"][0], (
            "cycle did not run"
        )

        from agent_mcp.repositories import rag_repo

        watermark = rag_repo.get_last_indexed("context")
        assert watermark == _GOOD2_TS, (
            f"clean cycle did not advance watermark to newest (got "
            f"{watermark!r})"
        )
