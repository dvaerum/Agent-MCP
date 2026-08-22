# Agent-MCP/tests/test_r13_f3_completion_client_cache.py
"""R13-F3 (MEDIUM, CONFIRMED, live-exploited): ``completion_client()``
constructed a brand-new client — and, on first ``chat()`` call, a brand
new ``openai.AsyncOpenAI`` SDK client with its own independent httpx
connection pool — on EVERY call, with nothing anywhere in the codebase
ever calling ``.aclose()`` on the transient clients (grep-confirmed).
R12-F3 fixed the identical class of bug on the sibling embedding seam
(``embedding_service.embedding_client()``) but the fix was never
actually applied here, despite this module's own docstring claiming the
two seams are "kept in lockstep". Live ``ss -tnp`` on the vm-dev guest
showed 11-25 stable ESTABLISHED sockets to the LLM endpoint, not
draining — every ``ask_project_rag`` call (``features/rag/query.py``)
resolves a fresh ``completion_client()``.

Fix direction (mirrors embedding_service.py's module-level comment
above its ``_client_cache`` for the full rationale): a shared cache
keyed on the resolved ``(class, model, base_url, api_key)`` tuple —
completion clients have no "dimension" concept, unlike embeddings.

These tests prove the cache actually holds: two calls with the SAME
resolved configuration must return the IDENTICAL client instance (so
its lazily-built SDK client / connection pool is reused, not
duplicated), while two calls with a DIFFERENT configuration must NOT
collide on the same cached instance (a runtime reconfigure must still
take effect, not get pinned to whatever was resolved first).
"""

from __future__ import annotations

import pytest


def test_completion_client_reuses_cached_instance_for_same_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same (provider, model, base_url, api_key) -> the SAME client
    object, so its underlying connection pool is reused rather than a
    fresh one leaking on every call."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("AGENT_MCP_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    from agent_mcp.external.completion_service import completion_client

    first = completion_client()
    second = completion_client()

    assert first is second, (
        "completion_client() built a NEW client on a repeat call with the "
        "same resolved configuration -- R13-F3: each call (and its "
        "lazily-built openai.AsyncOpenAI SDK client + httpx connection "
        "pool) leaks an independent connection with no cache and no "
        ".aclose() anywhere in the codebase."
    )


def test_completion_client_reuses_cached_instance_across_many_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N calls under an unchanged configuration -> exactly ONE distinct
    client instance, not N -- the direct behavioural claim from the
    finding (11-25 ESTABLISHED connections, not draining)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    from agent_mcp.external.completion_service import completion_client

    clients = [completion_client() for _ in range(25)]
    distinct_ids = {id(c) for c in clients}

    assert len(distinct_ids) == 1, (
        f"25 completion_client() calls under one unchanged configuration "
        f"produced {len(distinct_ids)} distinct client instances -- each "
        "one leaks its own connection pool with nothing to ever close it "
        "(R13-F3)."
    )


def test_completion_client_cache_keyed_on_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching provider (Ollama <-> OpenAI) must NOT reuse the other
    provider's cached client -- a runtime reconfigure has to actually
    take effect."""
    from agent_mcp.external.completion_service import (
        OllamaChatClient,
        OpenAIChatClient,
        completion_client,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "")
    ollama_client = completion_client()
    assert isinstance(ollama_client, OllamaChatClient)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    openai_client = completion_client()
    assert isinstance(openai_client, OpenAIChatClient)

    assert ollama_client is not openai_client, (
        "completion_client() returned the previous provider's cached "
        "client after OPENAI_API_KEY changed -- a runtime reconfigure "
        "must resolve a fresh (and separately cached) client for the "
        "new configuration."
    )

    # Switching back resolves the ORIGINAL cached Ollama client again --
    # proves the cache holds multiple live configurations rather than
    # evicting on every resolution change.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert completion_client() is ollama_client


def test_completion_client_cache_keyed_on_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct AGENT_MCP_LLM_BASE_URL overrides for the same
    (Ollama) provider must NOT collide on one cached client -- each
    endpoint gets its own connection pool."""
    monkeypatch.setenv("OPENAI_API_KEY", "")

    from agent_mcp.external.completion_service import completion_client

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-a.internal:9999/v1")
    client_a = completion_client()

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-b.internal:9999/v1")
    client_b = completion_client()

    assert client_a is not client_b
    assert client_a.base_url == "http://ollama-a.internal:9999/v1"
    assert client_b.base_url == "http://ollama-b.internal:9999/v1"

    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://ollama-a.internal:9999/v1")
    assert completion_client() is client_a


def test_completion_client_cache_keyed_on_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct OPENAI_MODEL values for the same provider must NOT
    collide on one cached client."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    from agent_mcp.external.completion_service import completion_client

    monkeypatch.setenv("OPENAI_MODEL", "gpt-model-a")
    client_a = completion_client()

    monkeypatch.setenv("OPENAI_MODEL", "gpt-model-b")
    client_b = completion_client()

    assert client_a is not client_b
    assert client_a.model == "gpt-model-a"
    assert client_b.model == "gpt-model-b"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-model-a")
    assert completion_client() is client_a
