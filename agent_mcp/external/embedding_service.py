# Agent-MCP/agent_mcp/external/embedding_service.py
"""Provider-agnostic text-embedding abstraction.

The sibling of :mod:`agent_mcp.external.completion_service`. Chat
completion already had a clean provider seam — ``completion_client()``
branches OpenAI-vs-Ollama and hands back a client behind
``_BaseChatClient.chat()``. *Embedding* — the identical
OpenAI-vs-Ollama problem — had no seam: "turn text into a vector" was
assembled inline at four RAG call sites, each re-typing
``model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSION`` and each
resolving the endpoint DIFFERENTLY:

* ``features/rag/indexing.py`` built ``AsyncOpenAI(api_key=...)`` with
  **no** ``base_url`` — trusting the SDK's implicit ``OPENAI_BASE_URL``
  env pickup that ``completion_service`` deliberately distrusts.
* ``features/rag/query.py`` / ``index_task_data`` went through
  ``openai_service.get_openai_client()`` — a different resolution path.

Two paths → a real Ollama-vs-OpenAI divergence. This module is the one
seam that owns ``(model, dimension, base_url, api_key)`` and exposes an
``embed(texts) -> list[vector]`` (plus its async twin ``aembed`` for the
concurrent index-batch path).

Design (mirrors completion_service)
-----------------------------------

Branch on ``OPENAI_API_KEY`` exactly like ``completion_client()`` so
the two seams always agree on which provider is live:

* **Set**   → :class:`OpenAIEmbeddingClient`. ``base_url`` is resolved
  EXPLICITLY from ``OPENAI_BASE_URL`` (or ``None`` for the cloud
  default) — never left to the SDK's implicit env pickup, which is the
  divergence this refactor closes.
* **Unset / empty** → :class:`OllamaEmbeddingClient`, pointed at
  ``AGENT_MCP_LLM_BASE_URL`` (default ``http://localhost:11434/v1``),
  with the ``"ollama"`` api-key sentinel the SDK requires but Ollama
  ignores.

Unlike ``completion_client()`` there is no "key set but model unset"
fail-fast: the embedding model/dimension always resolve to a
``core.config`` default (``EMBEDDING_MODEL`` / ``EMBEDDING_DIMENSION``),
so there is no ambiguous-config trap to guard against.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from ..core import config as _config
from ..core.config import logger


_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


class EmbeddingConfigError(RuntimeError):
    """Raised when the ``openai`` SDK cannot be imported to build a client."""


class _BaseEmbeddingClient:
    """Shared scaffolding for the provider-specific subclasses.

    Holds the model name + dimension + an ``openai`` client bound to the
    right base URL & API key. The actual SDK call lives in
    :meth:`embed` / :meth:`aembed` so both subclasses share the
    response-shape unpacking (``.data[i].embedding``) exactly once.
    """

    provider: str = "unknown"

    def __init__(
        self,
        model: str,
        dimension: int,
        *,
        base_url: Optional[str],
        api_key: str,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self.base_url = base_url
        self._api_key = api_key
        # Sync + async clients are distinct SDK objects; build each
        # lazily (the openai import is heavy and some callers only need
        # one of the two).
        self._sync_client: Any = None
        self._async_client: Any = None

    def _import_openai(self) -> Any:
        try:
            import openai  # local import: keeps module-load cheap
        except ImportError as e:  # pragma: no cover — openai is a hard dep
            raise EmbeddingConfigError(
                "openai package not installed; cannot construct embedding client"
            ) from e
        return openai

    def _client_kwargs(self) -> dict:
        kwargs: dict = {"api_key": self._api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    def _get_sync_client(self) -> Any:
        if self._sync_client is None:
            self._sync_client = self._import_openai().OpenAI(**self._client_kwargs())
        return self._sync_client

    def _get_async_client(self) -> Any:
        if self._async_client is None:
            self._async_client = self._import_openai().AsyncOpenAI(
                **self._client_kwargs()
            )
        return self._async_client

    @staticmethod
    def _unpack(response: Any) -> List[List[float]]:
        try:
            return [item.embedding for item in response.data]
        except (AttributeError, TypeError) as e:
            raise RuntimeError(
                f"embedding client: unexpected response shape: {response!r}"
            ) from e

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Synchronously embed ``texts``; returns one vector per input,
        in order."""
        client = self._get_sync_client()
        response = client.embeddings.create(
            input=texts,
            model=self.model,
            dimensions=self.dimension,
        )
        return self._unpack(response)

    async def aembed(self, texts: List[str]) -> List[List[float]]:
        """Async twin of :meth:`embed`, used by the concurrent
        index-batch path so each batch can run in its own task."""
        client = self._get_async_client()
        response = await client.embeddings.create(
            input=texts,
            model=self.model,
            dimensions=self.dimension,
        )
        return self._unpack(response)


class OpenAIEmbeddingClient(_BaseEmbeddingClient):
    """Embeds against OpenAI cloud (or any compatible endpoint).

    ``base_url`` is resolved EXPLICITLY from ``OPENAI_BASE_URL`` rather
    than trusting the SDK's implicit env pickup — the divergence this
    module closes.
    """

    provider = "openai"

    def __init__(self, model: str, dimension: int) -> None:
        api_key = os.environ.get("OPENAI_API_KEY") or ""
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        super().__init__(model, dimension, base_url=base_url, api_key=api_key)


class OllamaEmbeddingClient(_BaseEmbeddingClient):
    """Embeds against a local Ollama OpenAI-compatible endpoint.

    Defaults to ``http://localhost:11434/v1``; override with
    ``AGENT_MCP_LLM_BASE_URL`` (same knob :class:`OllamaChatClient`
    honours). The ``api_key`` is the ``"ollama"`` sentinel the SDK
    requires but Ollama ignores.
    """

    provider = "ollama"

    def __init__(self, model: str, dimension: int) -> None:
        base_url = (
            os.environ.get("AGENT_MCP_LLM_BASE_URL", "").strip()
            or _OLLAMA_DEFAULT_BASE_URL
        )
        super().__init__(model, dimension, base_url=base_url, api_key="ollama")


EmbeddingClient = _BaseEmbeddingClient  # Public type alias for callers


def embedding_client(
    *,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
) -> _BaseEmbeddingClient:
    """Pick an embedding client based on env vars.

    Branches OpenAI-vs-Ollama on ``OPENAI_API_KEY`` — the SAME switch
    ``completion_client()`` uses, so the completion and embedding seams
    never disagree about which provider is live. ``model`` / ``dimension``
    default to the ``core.config`` values (read at call time so a runtime
    reconfigure is honoured) but may be overridden by callers.
    """
    resolved_model = model or _config.EMBEDDING_MODEL
    resolved_dimension = (
        dimension if dimension is not None else _config.EMBEDDING_DIMENSION
    )

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        logger.debug(
            "embedding_service: using OpenAI provider model=%s dim=%s",
            resolved_model,
            resolved_dimension,
        )
        return OpenAIEmbeddingClient(resolved_model, resolved_dimension)

    logger.debug(
        "embedding_service: using Ollama provider model=%s dim=%s",
        resolved_model,
        resolved_dimension,
    )
    return OllamaEmbeddingClient(resolved_model, resolved_dimension)
