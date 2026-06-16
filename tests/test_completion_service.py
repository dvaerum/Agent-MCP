"""Provider-selection tests for the completion-service abstraction.

The completion service decides whether RAG completion calls go to
OpenAI cloud or a local Ollama endpoint, based on two env vars:

  * ``OPENAI_API_KEY`` set    → OpenAI; requires ``OPENAI_MODEL``
  * ``OPENAI_API_KEY`` unset  → Ollama; uses ``OLLAMA_MODEL`` (default
                                 ``qwen3:1.7b``) and
                                 ``AGENT_MCP_LLM_BASE_URL`` (default
                                 ``http://localhost:11434/v1``)

This file pins the selection logic so future refactors don't silently
re-introduce the v5.0.43 ``gpt-4.1-2025-04-14`` hardcode that broke RAG
on VM deployments with no OpenAI key. Locked design decision —
Dennis 2026-06-16.
"""

from __future__ import annotations

import pytest


def test_no_openai_key_returns_ollama_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an OpenAI key, callers transparently get Ollama."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    from agent_mcp.external.completion_service import (
        OllamaChatClient,
        completion_client,
    )

    client = completion_client()
    assert isinstance(client, OllamaChatClient)
    # Default model when OLLAMA_MODEL is not set — matches the chat
    # model the VM downloads, so the self-contained e2e flow works
    # out of the box.
    assert client.model == "qwen3:1.7b"


def test_no_openai_key_respects_ollama_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can pick a different local model via OLLAMA_MODEL."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:1b")

    from agent_mcp.external.completion_service import (
        OllamaChatClient,
        completion_client,
    )

    client = completion_client()
    assert isinstance(client, OllamaChatClient)
    assert client.model == "llama3.2:1b"


def test_empty_openai_key_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conftest._isolate_env sets OPENAI_API_KEY=""; that means "no key"."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    from agent_mcp.external.completion_service import (
        OllamaChatClient,
        completion_client,
    )

    assert isinstance(completion_client(), OllamaChatClient)


def test_openai_key_with_model_returns_openai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both env vars set, callers get an OpenAI client wired to that model."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")

    from agent_mcp.external.completion_service import (
        OpenAIChatClient,
        completion_client,
    )

    client = completion_client()
    assert isinstance(client, OpenAIChatClient)
    assert client.model == "gpt-4o-mini"


def test_openai_key_without_model_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting only the API key is a configuration error — fail fast.

    The previous behavior was to silently fall back to a hardcoded
    ``gpt-4.1-2025-04-14`` which doesn't exist in OpenAI's catalog.
    Removing the default forces deployments to be explicit.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    from agent_mcp.external.completion_service import (
        CompletionConfigError,
        completion_client,
    )

    with pytest.raises(CompletionConfigError):
        completion_client()


def test_openai_key_with_empty_model_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty OPENAI_MODEL is equivalent to unset — same fail-fast."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("OPENAI_MODEL", "   ")

    from agent_mcp.external.completion_service import (
        CompletionConfigError,
        completion_client,
    )

    with pytest.raises(CompletionConfigError):
        completion_client()


def test_both_clients_expose_chat_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uniform interface contract: callers don't branch on provider."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from agent_mcp.external.completion_service import (
        OllamaChatClient,
        OpenAIChatClient,
    )

    # Construct both directly so we don't depend on env state.
    ollama = OllamaChatClient(model="qwen3:1.7b")
    openai_client = OpenAIChatClient(model="gpt-4o-mini")

    assert callable(getattr(ollama, "chat", None))
    assert callable(getattr(openai_client, "chat", None))
