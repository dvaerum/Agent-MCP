# Agent-MCP/agent_mcp/external/completion_service.py
"""Provider-agnostic chat-completion abstraction.

The v5.0.43 RAG path hardcoded ``gpt-4.1-2025-04-14`` for both the
chat-completion synthesis step and the task-placement analysis step.
That model name doesn't exist in OpenAI's catalog (was a typo), and
deployments without an ``OPENAI_API_KEY`` had no fallback at all —
RAG completion just failed on the VM. v5.0.44 routes both call sites
through this module.

Design (locked by Dennis 2026-06-16)
------------------------------------

Branch on ``OPENAI_API_KEY``:

* **Unset**   → :class:`OllamaChatClient`, model from ``OLLAMA_MODEL``
  (default ``qwen3:1.7b`` — matches the chat model the VM downloads
  out-of-the-box so RAG works self-contained).
* **Set** + ``OPENAI_MODEL`` set → :class:`OpenAIChatClient` with that
  model.
* **Set** + ``OPENAI_MODEL`` unset → :class:`CompletionConfigError` at
  startup. No silent fallback to a "default" model — picking the wrong
  one (as v5.0.43 did) is worse than failing fast.

This is a breaking change for deploys that relied on the hardcoded
default. Anyone with ``OPENAI_API_KEY`` set must now also set
``OPENAI_MODEL``.

Why two classes (vs. one OpenAI-SDK wrapper)?
---------------------------------------------

Both ultimately speak the OpenAI ``/v1/chat/completions`` wire format
— Ollama exposes an OpenAI-compatible API on port 11434. We use the
``openai`` SDK for both. The two classes exist to make the provider
choice legible at the call site (``isinstance`` checks in tests, log
lines that say *"using Ollama"*, etc.) and to keep each provider's
default base URL / API-key sentinel in one place. The shared
``chat()`` method delegates to the same SDK under the hood.

Why not asyncio.run() in callers?
---------------------------------

The two existing call sites (``features/rag/query.py``,
``features/task_placement/validator.py``) both happen inside async
coroutines already. ``chat()`` is async; callers ``await`` it
directly. Sync callers can use :func:`asyncio.run` themselves if they
ever appear; none do today.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from ..core.config import logger


_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
# Default chat model bundled with the VM. Picked for size: ~1 GB
# download fits the 8 GB qcow2 alongside the embedding model and
# completes in a reasonable first-boot window. Operators can override
# via OLLAMA_MODEL.
_OLLAMA_DEFAULT_MODEL = "qwen3:1.7b"


class CompletionConfigError(RuntimeError):
    """Raised when env-var configuration is internally inconsistent.

    The one current trigger is ``OPENAI_API_KEY`` set without an
    accompanying ``OPENAI_MODEL`` — see module docstring.
    """


class _BaseChatClient:
    """Shared scaffolding for the provider-specific subclasses.

    Holds the model name + an instantiated ``openai.AsyncOpenAI``
    bound to the right base URL & API key. The actual SDK call lives
    in :meth:`chat` so both subclasses share the response-shape
    unpacking logic.
    """

    provider: str = "unknown"

    def __init__(self, model: str, *, base_url: Optional[str], api_key: str) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._client: Any = None  # Lazily constructed; openai import is heavy.

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import openai  # local import: keeps module-load cheap
        except ImportError as e:  # pragma: no cover — openai is a hard dep
            raise CompletionConfigError(
                "openai package not installed; cannot construct chat client"
            ) from e
        kwargs: dict = {"api_key": self._api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    async def chat(
        self,
        messages: List[dict],
        *,
        temperature: float = 0.4,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send a chat-completion request, return the assistant text.

        ``messages`` is the standard OpenAI list-of-dicts shape:
        ``[{"role": "system"|"user"|"assistant", "content": "..."}]``.
        """
        client = self._get_client()
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await client.chat.completions.create(**kwargs)
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as e:
            raise RuntimeError(
                f"completion client {self.provider}: unexpected response shape: {response!r}"
            ) from e
        return content or ""


class OpenAIChatClient(_BaseChatClient):
    """Talks to OpenAI cloud (or any compatible endpoint via OPENAI_BASE_URL).

    Reads the API key from ``OPENAI_API_KEY`` at construction. The
    ``openai`` SDK also honours ``OPENAI_BASE_URL`` from the environment
    if we pass ``base_url=None``, so deployments that re-point the SDK
    at a private gateway via that env var keep working transparently.
    """

    provider = "openai"

    def __init__(self, model: str) -> None:
        api_key = os.environ.get("OPENAI_API_KEY") or ""
        # base_url=None ⇒ SDK picks up OPENAI_BASE_URL itself (or the
        # cloud default if unset). Don't second-guess it here.
        super().__init__(model=model, base_url=None, api_key=api_key)


class OllamaChatClient(_BaseChatClient):
    """Talks to a local Ollama OpenAI-compatible endpoint.

    Defaults to ``http://localhost:11434/v1``; override with
    ``AGENT_MCP_LLM_BASE_URL``. The ``api_key`` argument is required by
    the SDK but Ollama ignores it — we pass the sentinel string
    ``"ollama"`` so logs make the intent obvious.
    """

    provider = "ollama"

    def __init__(self, model: str) -> None:
        base_url = (
            os.environ.get("AGENT_MCP_LLM_BASE_URL", "").strip()
            or _OLLAMA_DEFAULT_BASE_URL
        )
        super().__init__(model=model, base_url=base_url, api_key="ollama")


CompletionClient = _BaseChatClient  # Public type alias for callers


def completion_client() -> _BaseChatClient:
    """Pick a chat-completion client based on env vars.

    See module docstring for the full decision table. Raises
    :class:`CompletionConfigError` when ``OPENAI_API_KEY`` is set
    without ``OPENAI_MODEL``.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        model = os.environ.get("OPENAI_MODEL", "").strip()
        if not model:
            raise CompletionConfigError(
                "OPENAI_API_KEY is set but OPENAI_MODEL is not. "
                "v5.0.44 removed the hardcoded 'gpt-4.1-2025-04-14' default; "
                "deployments using the OpenAI provider must now declare the "
                "model explicitly via the OPENAI_MODEL env var."
            )
        logger.info("completion_service: using OpenAI provider with model=%s", model)
        return OpenAIChatClient(model=model)

    model = (
        os.environ.get("OLLAMA_MODEL", "").strip() or _OLLAMA_DEFAULT_MODEL
    )
    logger.info("completion_service: using Ollama provider with model=%s", model)
    return OllamaChatClient(model=model)
