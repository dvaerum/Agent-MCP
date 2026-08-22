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
fail-fast: the embedding model/dimension always resolve to
``core.config.embedding_settings()`` — called fresh at each invocation
so a runtime ``--advanced`` reconfigure is honoured — so there is no
ambiguous-config trap to guard against.
"""

from __future__ import annotations

import os
import threading
from typing import Any, List, Optional

from ..core import config as _config
from ..core.config import logger


_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"

# R12-F2 (HIGH, live-exploited) defense-in-depth: the openai SDK's own
# default is 600s x automatic retries. Even after fixing every call
# site to await the async client (the actual fix for the event-loop
# freeze — see query.py / indexing.py), an unreachable/hung provider
# should still degrade in a bounded number of seconds, not minutes.
# Mirrors the identically-named constant in completion_service.py by
# design (see that module's docstring on why the two seams are kept in
# lockstep but not merged).
_SDK_CLIENT_TIMEOUT_SECONDS = float(
    os.environ.get("AGENT_MCP_LLM_CLIENT_TIMEOUT_SECONDS", "30")
)


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
        kwargs: dict = {
            "api_key": self._api_key,
            "timeout": _SDK_CLIENT_TIMEOUT_SECONDS,
        }
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
        in order.

        R12-F2 (HIGH, live-exploited): NEVER call this from inside a
        coroutine running on the shared server event loop. It's a
        genuinely blocking network round-trip (bounded by
        ``_SDK_CLIENT_TIMEOUT_SECONDS`` but still seconds-long) with no
        ``await`` point for the loop to yield on, so it freezes every
        other coroutine on that loop — all REST endpoints, every other
        agent's stream, task/message ops — for the call's whole
        duration. Every async call site MUST use :meth:`aembed`
        instead (``await embedding_client().aembed(...)``); this
        method exists for genuinely synchronous callers only (e.g. a
        plain script, or code already off-loaded to a worker thread).
        """
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


# R12-F3 (MEDIUM, CONFIRMED): every embedding_client() call used to
# construct a BRAND NEW client — and, the first time embed()/aembed()
# is actually invoked on it, a brand new openai SDK client with its own
# independent httpx connection pool — with nothing anywhere in the
# codebase ever calling .close()/.aclose(). Live SSH showed 21
# simultaneous ESTABLISHED connections to the embedding endpoint after
# ~1hr uptime, monotonically accumulating with every embedding
# operation: unbounded per-process FD/connection growth that also
# compounds R12-F2 (new calls queue behind an ever-growing set of stale
# connections to the same provider).
#
# Fix: cache one client per resolved (class, model, dimension, base_url,
# api_key) tuple at module scope, so a long-running server process
# reuses the SAME client — and therefore the SAME underlying connection
# pool — across every call with that configuration, instead of leaking
# a fresh one each time. A cache (vs. threading .close() calls through
# every call site) is the simpler fix here: it needs no lifecycle
# management at any call site, and the underlying _BaseEmbeddingClient
# already lazily builds its SDK client exactly once (see
# _get_sync_client / _get_async_client) — this just extends that same
# "build once, reuse" discipline one level up, to the client object
# itself. Distinct tuples (e.g. an operator's live --advanced
# reconfigure, or an explicit model/dimension override) still get their
# own cached entry, so a runtime reconfigure is honoured exactly as
# before — nothing is pinned to the FIRST resolution.
#
# Constructing a candidate instance is cheap and side-effect-free
# (attribute assignment only; __init__ never touches the network or
# builds an SDK client — see _get_sync_client/_get_async_client's own
# lazy construction), so building one to read back its OWN resolved
# base_url/api_key for the cache key is simpler and less error-prone
# than re-deriving that resolution logic (which each subclass's
# __init__ already owns) a second time here.
_client_cache: dict[tuple, _BaseEmbeddingClient] = {}
_client_cache_lock = threading.Lock()


def reset_embedding_client_cache() -> None:
    """Drop every cached client. Test-isolation seam (mirrors
    ``db.engine.reset_engine_cache()``'s per-test reset) — a real
    server process never needs to call this."""
    with _client_cache_lock:
        _client_cache.clear()


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

    R12-F3: returns a CACHED client (and therefore a shared connection
    pool) for a given (provider, model, dimension, base_url, api_key)
    tuple rather than a fresh one on every call — see the module-level
    comment above ``_client_cache`` for why.
    """
    settings = _config.embedding_settings()
    resolved_model = model or settings.model
    resolved_dimension = dimension if dimension is not None else settings.dimension

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        logger.debug(
            "embedding_service: using OpenAI provider model=%s dim=%s",
            resolved_model,
            resolved_dimension,
        )
        candidate: _BaseEmbeddingClient = OpenAIEmbeddingClient(
            resolved_model, resolved_dimension
        )
    else:
        logger.debug(
            "embedding_service: using Ollama provider model=%s dim=%s",
            resolved_model,
            resolved_dimension,
        )
        candidate = OllamaEmbeddingClient(resolved_model, resolved_dimension)

    cache_key = (
        type(candidate),
        candidate.model,
        candidate.dimension,
        candidate.base_url,
        candidate._api_key,
    )
    with _client_cache_lock:
        cached = _client_cache.get(cache_key)
        if cached is not None:
            return cached
        _client_cache[cache_key] = candidate
        return candidate
