"""arch-r4 #2 (HIGH, latent correctness bug): the RAG indexer's writer
must resolve "can I embed?" through the SAME provider seam the reader
(``features/rag/query.py``) uses — otherwise an Ollama-only deploy can
QUERY the vector index but never WRITES to it.

FINDING: ``run_rag_indexing_periodically`` used to hard-abort
(``if not openai_api_key_for_batches: logger.error(...); return``)
whenever ``OPENAI_API_KEY`` was falsy — including the empty-string
convention many ``.env`` files use for "no OpenAI key, use the bundled
Ollama default" (see ``core.config``'s own ``OPENAI_API_KEY``
sentinel-seeding). But ``embedding_client()`` — the seam BOTH
``indexing.py`` and ``query.py`` actually embed through — resolves to
Ollama just fine with no key at all, exactly like ``query.py`` already
assumes (it has no such guard). So an Ollama-only deploy could QUERY
the vec index but the WRITER bailed out on cycle one, leaving the
index permanently empty.

Why a hand-rolled fake embedding client instead of the ``mock_ollama``
fixture: the on-disk ``rag_embeddings`` vec0 column is created from
``embedding_settings().dimension`` (currently 1536 in a bare test
process — a PRE-EXISTING, separate ordering quirk where
``SIMPLE_EMBEDDING_DIMENSION`` is read before ``core.config``'s own
Ollama-default env-var seeding runs, so the seeded 1024 default never
actually reaches it). ``mock_ollama`` always answers with a hardcoded
1024-dim vector, which collides with that unrelated quirk and raises a
sqlite-vec "Dimension mismatch" error unrelated to the guard this test
targets. Patching ``embedding_client()`` directly keeps this test
hermetic and focused on ONE thing: does the indexer's own
OPENAI_API_KEY gate block it from ever reaching the embedding seam?

RED on unfixed code: this test seeds a markdown file, runs one real
indexer cycle with ``OPENAI_API_KEY`` unset (the ``conftest``
default), then queries the index back through the same seam. Pre-fix,
the indexer returns before its first cycle — via the OPENAI_API_KEY
guard, before ever calling ``embedding_client()`` — and the query
finds nothing. Post-fix, the doc is embedded and the query returns it.
"""

from __future__ import annotations

import anyio
import pytest

from agent_mcp.features.rag import indexing as indexing_mod
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio

_MARKER = "arch-r4-2-liveness-marker-9f3a2c"


class _FakeEmbeddingClient:
    """Deterministic same-dimension-as-the-real-table stand-in for
    ``embedding_client()``. Exercises the exact ``embed``/``aembed``
    interface both the indexer and the query path call — see
    ``external.embedding_service._BaseEmbeddingClient``."""

    provider = "fake"

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


async def test_ollama_only_deploy_indexes_and_queries_markdown(
    tmp_path, monkeypatch
) -> None:
    """One real indexer cycle, no OpenAI key anywhere, must populate the
    vector index — and a query through the same seam must find it."""
    async with mcp_session(tmp_path):
        from agent_mcp.core import globals as g
        from agent_mcp.core.config import embedding_settings
        from agent_mcp.db.connection import is_vss_loadable

        if not is_vss_loadable():
            pytest.skip("sqlite-vec not loadable on this host")

        # ``OPENAI_API_KEY_ENV`` is a module constant frozen at
        # core.config's FIRST import in this process — which may have
        # seen a different ambient OPENAI_API_KEY than "" (e.g. fully
        # unset, which core.config's own Ollama-default seeding turns
        # into the truthy sentinel "ollama"). Force it directly so this
        # test deterministically reproduces the real-world trigger: an
        # operator's `.env`/systemd EnvironmentFile setting
        # `OPENAI_API_KEY=` (present, empty) rather than leaving the
        # var fully unset.
        monkeypatch.setattr("agent_mcp.core.config.OPENAI_API_KEY_ENV", "")

        fake_client = _FakeEmbeddingClient(embedding_settings().dimension)
        monkeypatch.setattr(indexing_mod, "embedding_client", lambda: fake_client)

        # mcp_session builds its app against ``tmp_path / "project"``
        # (see harness.py's module docstring) — write the marker doc
        # straight into it so the indexer's glob scan finds it.
        project_dir = tmp_path / "project"
        (project_dir / "ARCH_R4_2_NOTES.md").write_text(
            f"# Ollama Liveness Marker\n\n{_MARKER}: this paragraph must "
            "be retrievable after a real Ollama-backed indexing cycle.\n"
        )

        # Bound the indexer to exactly one cycle, mirroring
        # test_sec_r31_rag_watermark.py's pattern: the only >=30s sleep
        # is the end-of-cycle boundary.
        async def fake_sleep(duration):
            if duration >= 30:
                g.server_running = False

        monkeypatch.setattr(anyio, "sleep", fake_sleep)
        g.server_running = True
        g.startup_complete_event.set()

        # OPENAI_API_KEY is "" here (conftest._isolate_env / mcp_session
        # both force it) — the exact condition that used to hard-abort
        # the indexer before its first cycle.
        await indexing_mod.run_rag_indexing_periodically(interval_seconds=300)

        from agent_mcp.repositories import rag_repo

        # Query through the SAME seam the indexer just wrote through —
        # exactly what "one answer for read and write" means.
        query_vector = fake_client.embed([_MARKER])[0]
        results = rag_repo.search_similar(query_embedding=query_vector, limit=5)

        assert results, (
            "RAG vector index is empty after an Ollama-mode indexing "
            "cycle — the writer bailed out on the OPENAI_API_KEY guard "
            "before ever calling embedding_client()"
        )
        assert any(_MARKER in (r.get("chunk_text") or "") for r in results), (
            "the marker document was not embedded/retrievable after the "
            "indexing cycle"
        )
