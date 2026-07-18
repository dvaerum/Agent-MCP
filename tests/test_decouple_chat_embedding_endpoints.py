"""Decouple the chat/completion endpoint from the embedding endpoint.

A local deployment runs embeddings on a CPU Ollama (``:11434``, which
serves the embedding model) but wants RAG answer-generation to run on a
fast iGPU llama-cpp (``:11435``). ``core.config`` sets ``OPENAI_API_KEY``
to the sentinel ``"ollama"`` when unset, so ``completion_client()``
always takes the OpenAI-provider path — which passed ``base_url=None``
and therefore locked chat to the SAME ``OPENAI_BASE_URL`` the embedding
client resolves from. There was no way to split them.

The fix makes ``AGENT_MCP_LLM_BASE_URL`` the "chat/completion endpoint"
knob for BOTH providers (overriding ``base_url`` when set), while
``OPENAI_BASE_URL`` stays the "embedding (+SDK fallback) endpoint" — so
the two can point at different hosts. Unset preserves today's behaviour
(``base_url=None`` ⇒ SDK falls back to ``OPENAI_BASE_URL``).
"""

from __future__ import annotations

import pytest


def test_openai_completion_honors_llm_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel-key (local Ollama) path: AGENT_MCP_LLM_BASE_URL, when set,
    overrides the chat endpoint so RAG answers can go to the iGPU
    llama-cpp on :11435."""
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")  # core.config sentinel
    monkeypatch.setenv("OPENAI_MODEL", "qwen2.5:3b-instruct")
    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://localhost:11435/v1")

    from agent_mcp.external.completion_service import (
        OpenAIChatClient,
        completion_client,
    )

    client = completion_client()
    assert isinstance(client, OpenAIChatClient)
    assert client.base_url == "http://localhost:11435/v1"


def test_openai_completion_base_url_none_when_override_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset AGENT_MCP_LLM_BASE_URL preserves today's behaviour:
    base_url=None so the SDK falls back to OPENAI_BASE_URL."""
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("OPENAI_MODEL", "qwen2.5:3b-instruct")
    monkeypatch.delenv("AGENT_MCP_LLM_BASE_URL", raising=False)

    from agent_mcp.external.completion_service import completion_client

    assert completion_client().base_url is None


def test_embedding_endpoint_decoupled_from_chat_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the change: chat and embeddings target DIFFERENT
    endpoints. With AGENT_MCP_LLM_BASE_URL pointed at the iGPU (:11435),
    embeddings still resolve from OPENAI_BASE_URL (:11434)."""
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("AGENT_MCP_LLM_BASE_URL", "http://localhost:11435/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")

    from agent_mcp.external.embedding_service import embedding_client

    assert embedding_client().base_url == "http://127.0.0.1:11434/v1"


def test_real_openai_key_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real cloud key with no AGENT_MCP_LLM_BASE_URL keeps base_url=None
    so the SDK talks to the OpenAI cloud default."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.delenv("AGENT_MCP_LLM_BASE_URL", raising=False)

    from agent_mcp.external.completion_service import completion_client

    assert completion_client().base_url is None
