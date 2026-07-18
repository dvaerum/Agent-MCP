"""Runtime discovery of the chat model's context window + budget derivation.

Local inference servers run a *fixed* per-slot context window (llama-cpp
``-c / -np``; Ollama ``num_ctx``). Overflowing it makes the completion
call 400 with ``exceed_context_size_error``. Rather than hardcode that
window per deploy, this module discovers it at runtime and derives every
local-model input budget from it — so budgets adapt automatically to
whatever host agent-mcp runs on, INCLUDING a host whose window is
smaller than ours.

Resolution order for the window (``resolve_context_window``):

1. ``AGENT_MCP_MODEL_CONTEXT_WINDOW`` — explicit override (also covers
   endpoints that can't be probed, e.g. Ollama, whose ``/api/show``
   reports the model's trained max, NOT the runtime ``num_ctx``).
2. A cached probe of ``GET {base_url}/props`` — llama-cpp exposes the
   live per-slot ``n_ctx`` there. One probe per endpoint per process
   (the window can't change while the server runs); any failure falls
   through.
3. ``_DEFAULT_WINDOW`` — a conservative fail-safe when nothing is
   reachable and no override is set.

Budgets are **override-on-top**: an explicit ``AGENT_MCP_MAX_CONTEXT_TOKENS``
still wins (unchanged behaviour); otherwise the budget is derived from
the discovered window minus reserved headroom.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import httpx

from ..core.config import _positive_int_env, logger

# Fallback for a malformed AGENT_MCP_MODEL_CONTEXT_WINDOW override value.
_DEFAULT_WINDOW = 4096

# When the window is UNDISCOVERABLE (no base_url, endpoint unreachable, or
# a non-llama-cpp endpoint such as cloud OpenAI / Ollama), the budget must
# stay exactly as it was before discovery existed: unbounded. This matches
# config.MAX_CONTEXT_TOKENS' historical default (GPT-4.1's window), so
# cloud deploys never regress. Discovery only ever TIGHTENS the budget,
# and only when it positively identifies a real (small) window.
_LEGACY_UNBOUNDED_BUDGET = 1_000_000

# Retrieved-context budget = window − headroom (system prompt + query +
# generated answer), floored so a tiny window still yields a usable budget.
_BUDGET_HEADROOM = 2048
_MIN_BUDGET = 1024

# Subject helper: a one-line subject only needs the OPENING of a message,
# so its input cap is CEILING-capped (never grows past this even on a huge
# window — feeding more wouldn't improve a 6-word subject) yet still
# auto-shrinks below the ceiling on a smaller window.
_SUBJECT_CHARS_CEILING = 4000
_SUBJECT_RESERVE_TOKENS = 128  # system prompt + 32-token output + margin
# Conservative chars/token: a SMALL value keeps the char cap safe (fewer
# chars per token budget) even for dense/code/CJK input.
_CHARS_PER_TOKEN = 2

_PROPS_TIMEOUT_S = 3.0

# Per-endpoint cache: the window is constant for a server's lifetime.
# We cache BOTH outcomes — a successful probe (int) and a failed one
# (None) — so a cloud endpoint without /props isn't re-probed (and made
# to eat the timeout) on every single query.
_WINDOW_CACHE: Dict[str, Optional[int]] = {}


async def _probe_props(base_url: str) -> Optional[int]:
    """GET ``{server}/props`` and read ``default_generation_settings.n_ctx``.

    llama-cpp's server exposes the live per-slot context size here. Any
    failure (endpoint down, non-llama-cpp server, unexpected shape)
    returns ``None`` so the caller falls back to the default.
    """
    root = base_url.rstrip("/")
    # base_url is an OpenAI-style ``.../v1``; /props sits at the server root.
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = root + "/props"
    try:
        async with httpx.AsyncClient(timeout=_PROPS_TIMEOUT_S) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        n_ctx = data.get("default_generation_settings", {}).get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            return n_ctx
        logger.debug("context-window probe: no n_ctx in /props at %s", url)
    except Exception as e:  # any transport / shape failure → default
        logger.debug("context-window probe failed for %s: %s", url, e)
    return None


async def resolve_context_window(base_url: Optional[str]) -> Optional[int]:
    """The chat model's usable per-slot context window in tokens, or
    ``None`` when it can't be determined (no ``base_url``, endpoint
    unreachable, or a non-llama-cpp endpoint). Callers pick their own
    no-regression fallback for the ``None`` case."""
    # 1. explicit override
    if os.environ.get("AGENT_MCP_MODEL_CONTEXT_WINDOW") is not None:
        return _positive_int_env("AGENT_MCP_MODEL_CONTEXT_WINDOW", _DEFAULT_WINDOW)
    # 2. cached probe (both success and failure are cached)
    if not base_url:
        return None
    if base_url in _WINDOW_CACHE:
        return _WINDOW_CACHE[base_url]
    probed = await _probe_props(base_url)
    _WINDOW_CACHE[base_url] = probed
    return probed


async def resolve_max_context_tokens(base_url: Optional[str]) -> int:
    """RAG retrieved-context budget. Override-on-top: an explicit
    ``AGENT_MCP_MAX_CONTEXT_TOKENS`` wins; otherwise derive from the
    discovered window, or stay unbounded (today's behaviour) if the window
    is undiscoverable — so cloud deploys never regress."""
    if os.environ.get("AGENT_MCP_MAX_CONTEXT_TOKENS") is not None:
        return _positive_int_env(
            "AGENT_MCP_MAX_CONTEXT_TOKENS", _LEGACY_UNBOUNDED_BUDGET
        )
    window = await resolve_context_window(base_url)
    if window is None:
        return _LEGACY_UNBOUNDED_BUDGET
    return max(_MIN_BUDGET, window - _BUDGET_HEADROOM)


async def resolve_subject_input_chars(base_url: Optional[str]) -> int:
    """Max characters of message body fed to the subject helper — small
    enough to never overflow the window, ceiling-capped because a subject
    only needs the opening. Falls back to the ceiling (today's fixed cap)
    when the window is undiscoverable."""
    window = await resolve_context_window(base_url)
    if window is None:
        return _SUBJECT_CHARS_CEILING
    safe_tokens = max(1, window - _SUBJECT_RESERVE_TOKENS)
    return min(_SUBJECT_CHARS_CEILING, safe_tokens * _CHARS_PER_TOKEN)
