"""R12-F3 (MEDIUM, CONFIRMED): ``embedding_client()`` constructed a
brand-new client — and, on first ``embed()``/``aembed()`` call, a brand
new ``openai`` SDK client with its own independent httpx connection
pool — on EVERY call site, with nothing anywhere in the codebase ever
calling ``.close()``/``.aclose()`` on the transient clients (grep-
confirmed). Live SSH showed 21 simultaneous ESTABLISHED connections to
the embedding endpoint after only ~1hr of uptime, monotonically
accumulating with every embedding operation: unbounded per-process
FD/connection growth that compounds R12-F2's freeze (new calls queue
behind an ever-growing set of stale connections to the same provider).

Fix direction chosen (see embedding_service.py's module-level comment
above ``_client_cache`` for the full rationale): a shared cache keyed
on the resolved ``(class, model, dimension, base_url, api_key)`` tuple,
rather than threading explicit ``.close()``/``.aclose()`` calls through
every call site. A cache needs no lifecycle management at any call
site and simply extends the ``_BaseEmbeddingClient``'s own existing
"build the SDK client lazily, once" discipline one level up to the
client object itself.

These tests prove the cache actually holds: two calls with the SAME
resolved configuration must return the IDENTICAL client instance (so
its lazily-built SDK client / connection pool is reused, not
duplicated), while two calls with a DIFFERENT configuration must NOT
collide on the same cached instance (a runtime reconfigure — e.g. an
operator's ``--advanced`` toggle, or an explicit model/dimension
override — must still take effect, not get pinned to whatever was
resolved first).
"""

from __future__ import annotations

import pytest


def test_embedding_client_reuses_cached_instance_for_same_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same (provider, model, dimension, base_url, api_key) -> the SAME
    client object, so its underlying connection pool is reused rather
    than a fresh one leaking on every call."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("AGENT_MCP_LLM_BASE_URL", raising=False)

    from agent_mcp.external.embedding_service import embedding_client

    first = embedding_client()
    second = embedding_client()

    assert first is second, (
        "embedding_client() built a NEW client on a repeat call with the "
        "same resolved configuration -- R12-F3: each call (and its "
        "lazily-built openai SDK client + httpx connection pool) leaks "
        "an independent connection with no cache and no .close()/"
        ".aclose() anywhere in the codebase."
    )


def test_embedding_client_reuses_cached_instance_across_many_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N calls under an unchanged configuration -> exactly ONE distinct
    client instance, not N — the direct behavioural claim from the
    finding (21 ESTABLISHED connections after ~1hr, monotonically
    accumulating)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    from agent_mcp.external.embedding_service import embedding_client

    clients = [embedding_client() for _ in range(25)]
    distinct_ids = {id(c) for c in clients}

    assert len(distinct_ids) == 1, (
        f"25 embedding_client() calls under one unchanged configuration "
        f"produced {len(distinct_ids)} distinct client instances -- each "
        "one leaks its own connection pool with nothing to ever close it "
        "(R12-F3)."
    )


def test_embedding_client_cache_keyed_on_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching provider (Ollama <-> OpenAI) must NOT reuse the other
    provider's cached client -- a runtime reconfigure has to actually
    take effect."""
    from agent_mcp.external.embedding_service import (
        OllamaEmbeddingClient,
        OpenAIEmbeddingClient,
        embedding_client,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "")
    ollama_client = embedding_client()
    assert isinstance(ollama_client, OllamaEmbeddingClient)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    openai_client = embedding_client()
    assert isinstance(openai_client, OpenAIEmbeddingClient)

    assert ollama_client is not openai_client, (
        "embedding_client() returned the previous provider's cached "
        "client after OPENAI_API_KEY changed -- a runtime reconfigure "
        "must resolve a fresh (and separately cached) client for the "
        "new configuration."
    )

    # Switching back resolves the ORIGINAL cached Ollama client again --
    # proves the cache holds multiple live configurations rather than
    # evicting on every resolution change.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert embedding_client() is ollama_client


def test_embedding_client_cache_keyed_on_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct AGENT_MCP_LLM_BASE_URL overrides for the same
    (Ollama) provider must NOT collide on one cached client -- each
    endpoint gets its own connection pool."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    from agent_mcp.external.embedding_service import embedding_client

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-a.internal:9999/v1")
    client_a = embedding_client()

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-b.internal:9999/v1")
    client_b = embedding_client()

    assert client_a is not client_b
    assert client_a.base_url == "http://ollama-a.internal:9999/v1"
    assert client_b.base_url == "http://ollama-b.internal:9999/v1"

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-a.internal:9999/v1")
    assert embedding_client() is client_a
