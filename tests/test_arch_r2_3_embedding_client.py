"""Arch round-2 #3 — the embedding provider seam mirrors the completion one.

Before this refactor, "turn text into a vector" was assembled inline at
four RAG call sites (``features/rag/indexing.py`` ×2,
``features/rag/query.py`` ×2), each re-typing ``model=EMBEDDING_MODEL,
dimensions=EMBEDDING_DIMENSION`` and each resolving the endpoint
DIFFERENTLY:

  * ``indexing.py`` built ``AsyncOpenAI(api_key=...)`` with NO
    ``base_url`` — trusting the SDK's implicit ``OPENAI_BASE_URL`` env
    pickup that ``completion_service`` deliberately distrusts.
  * ``query.py`` / ``index_task_data`` went through
    ``get_openai_client()`` — a different resolution path.

Two paths → a real Ollama-vs-OpenAI divergence. ``embedding_client()``
is the single seam: it owns ``(model, dimension, base_url, api_key)`` and
branches OpenAI-vs-Ollama on the SAME env vars ``completion_client()``
uses, so the Ollama embedding path is selected + assertable exactly like
``isinstance(cc, OllamaChatClient)`` — and ``base_url`` is honored
everywhere.
"""

from __future__ import annotations

import inspect

import pytest


# ── provider selection (mirror of test_completion_service) ───────────


def test_no_openai_key_returns_ollama_embedding_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an OpenAI key, callers transparently get the Ollama seam —
    the embedding analogue of ``isinstance(cc, OllamaChatClient)``.

    ``core.config`` must be imported *before* deleting the var: on its
    first import it runs ``os.environ.setdefault('OPENAI_API_KEY',
    'ollama')`` (its own local-default wiring), which would otherwise
    silently repopulate the key we just cleared. Importing it first lets
    the ``delenv`` stick, so we genuinely test the no-key branch."""
    from agent_mcp.external.embedding_service import (
        OllamaEmbeddingClient,
        embedding_client,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = embedding_client()
    assert isinstance(client, OllamaEmbeddingClient)
    assert client.provider == "ollama"


def test_empty_openai_key_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conftest._isolate_env sets OPENAI_API_KEY=""; that means "no key"."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    from agent_mcp.external.embedding_service import (
        OllamaEmbeddingClient,
        embedding_client,
    )

    assert isinstance(embedding_client(), OllamaEmbeddingClient)


def test_openai_key_returns_openai_embedding_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a key set, callers get the OpenAI seam."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    from agent_mcp.external.embedding_service import (
        OpenAIEmbeddingClient,
        embedding_client,
    )

    client = embedding_client()
    assert isinstance(client, OpenAIEmbeddingClient)
    assert client.provider == "openai"


# ── one endpoint-resolution rule: base_url is honored everywhere ─────


def test_ollama_base_url_defaults_and_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Ollama seam resolves ONE endpoint: the bundled default, or the
    operator override via AGENT_MCP_LLM_BASE_URL — never left implicit."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("AGENT_MCP_LLM_BASE_URL", raising=False)

    from agent_mcp.external.embedding_service import embedding_client

    assert embedding_client().base_url == "http://localhost:11434/v1"

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama.internal:9999/v1")
    assert embedding_client().base_url == "http://ollama.internal:9999/v1"


def test_openai_base_url_is_explicit_not_implicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OpenAI seam resolves OPENAI_BASE_URL EXPLICITLY (closing the
    indexing.py divergence that trusted the SDK's implicit env pickup)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.internal:8443/v1")

    from agent_mcp.external.embedding_service import embedding_client

    assert embedding_client().base_url == "http://gateway.internal:8443/v1"


def test_model_and_dimension_come_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam owns (model, dimension) — sourced from core.config so no
    call site re-types EMBEDDING_MODEL / EMBEDDING_DIMENSION."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    from agent_mcp.core import config as cfg
    from agent_mcp.external.embedding_service import embedding_client

    client = embedding_client()
    settings = cfg.embedding_settings()
    assert client.model == settings.model
    assert client.dimension == settings.dimension


# ── uniform interface contract ───────────────────────────────────────


def test_both_clients_expose_embed_and_aembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers don't branch on provider: sync ``embed`` + async ``aembed``
    exist on both adapters (batch path uses aembed; query path uses embed)."""
    from agent_mcp.external.embedding_service import (
        OllamaEmbeddingClient,
        OpenAIEmbeddingClient,
    )

    ollama = OllamaEmbeddingClient("m", 8)
    openai_c = OpenAIEmbeddingClient("m", 8)
    for c in (ollama, openai_c):
        assert callable(getattr(c, "embed", None))
        assert inspect.iscoroutinefunction(getattr(c, "aembed", None))


def test_embed_returns_list_of_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed(texts) -> list[vector]; adapts the OpenAI response shape once
    so no call site unpacks ``.data[i].embedding`` by hand."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from agent_mcp.external import embedding_service as es

    class _FakeResp:
        def __init__(self, n):
            self.data = [type("D", (), {"embedding": [0.1, 0.2]})() for _ in range(n)]

    class _FakeEmbeddings:
        def create(self, *, input, model, dimensions):
            return _FakeResp(len(input))

    class _FakeClient:
        embeddings = _FakeEmbeddings()

    client = es.embedding_client()
    monkeypatch.setattr(client, "_get_sync_client", lambda: _FakeClient())
    out = client.embed(["a", "b", "c"])
    assert out == [[0.1, 0.2], [0.1, 0.2], [0.1, 0.2]]


# ── all 4 call sites route through the seam (structural invariant) ───


def test_all_four_sites_use_the_seam_not_inline_assembly() -> None:
    """No RAG embedding site re-assembles the client inline: the batch
    embedder, index_task_data, and both query paths call the seam, and
    none constructs ``AsyncOpenAI(...)`` / calls ``.embeddings.create``
    directly."""
    from agent_mcp.features.rag import indexing as indexing_mod
    from agent_mcp.features.rag import query as query_mod

    batch_src = inspect.getsource(indexing_mod._get_embeddings_batch_openai)
    assert "embedding_client(" in batch_src
    assert "AsyncOpenAI(" not in batch_src

    task_src = inspect.getsource(indexing_mod.index_task_data)
    assert "embedding_client(" in task_src
    assert ".embeddings.create(" not in task_src

    for fn in (query_mod.query_rag_system, query_mod.query_rag_system_with_model):
        src = inspect.getsource(fn)
        assert "embedding_client(" in src, f"{fn.__name__} bypasses the seam"
        assert ".embeddings.create(" not in src, (
            f"{fn.__name__} still assembles the embedding call inline"
        )


@pytest.mark.asyncio
async def test_query_embedding_flows_through_the_seam(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural proof that query_rag_system's vector search resolves its
    query embedding via embedding_client() (not a private path)."""
    from agent_mcp.features.rag import query as query_mod

    calls = {"n": 0}

    class _Recorder:
        base_url = "http://recorded/v1"

        def embed(self, texts):
            calls["n"] += 1
            return [[0.0] * 8 for _ in texts]

    monkeypatch.setattr(query_mod, "embedding_client", lambda: _Recorder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: True)

    from agent_mcp.repositories import get_rag_repo

    monkeypatch.setattr(get_rag_repo(), "search_similar", lambda **kw: [])

    class _Chat:
        provider = "mock"
        model = "mock"

        async def chat(self, messages, temperature: float = 0.4) -> str:
            return "ok"

    monkeypatch.setattr(query_mod, "completion_client", lambda: _Chat())

    from tests.harness import mcp_session

    async with mcp_session(tmp_path):
        await query_mod.query_rag_system("does the embedding seam get used?")

    assert calls["n"] >= 1, "query embedding did not flow through embedding_client()"
