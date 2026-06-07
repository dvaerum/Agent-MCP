# Agent-MCP/agent_mcp/features/message_suggestions.py
"""Ollama-backed subject-suggestion helper for `agent_messages`.

The v5.0.22 message-threads feature lets root messages carry a
human-readable `subject`. When a sender doesn't supply one, this
module asks a local Ollama (or any OpenAI `/v1`-compatible) endpoint
to produce a one-line summary of the body.

Configuration
-------------
Two environment variables:

* ``AGENT_MCP_SUBJECT_MODEL`` — model name, e.g. ``qwen2.5:3b-instruct``.
  **If unset, `suggest_subject` returns ``None`` immediately** —
  callers fall back to truncated-body subjects so the server runs
  fine without an Ollama backend.
* ``AGENT_MCP_LLM_BASE_URL`` — base URL for the OpenAI-compatible
  API (default: ``http://localhost:11434/v1`` — the Ollama default).

Both are read on every call so operators can toggle them via systemd
unit reloads without restarting agent-mcp.

Why not the global `g.openai_client_instance`?
----------------------------------------------
The global sync client is bound to OpenAI cloud (api.openai.com)
with the operator's OPENAI_API_KEY. Pointing it at a local Ollama
would break RAG embeddings (which legitimately want the cloud
client). A per-call `openai.AsyncOpenAI(base_url=..., api_key="ollama")`
keeps the two pools fully separate, costs ~free (HTTP/2 keepalive
amortises the connect cost), and matches the convention the rag
indexer already uses (one `AsyncOpenAI` per batch — see
`features/rag/indexing.py`).

Failure mode
------------
Network errors, timeouts, unexpected response shapes, etc. all
collapse to ``None`` (with a debug log). The caller is expected to
fall back gracefully — typically to the same truncated-body path
used when the model isn't configured.
"""

from __future__ import annotations

import os
from typing import Optional

from ..core.config import logger


_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_SYSTEM_PROMPT = (
    "Summarize the user's message in 6 words or fewer as an email-style "
    "subject line. Return only the subject text, no quotes, no prefix, "
    "no punctuation at the end."
)
_MAX_SUBJECT_LEN = 80  # hard ceiling regardless of what the model returns


def _truncate(subject: str) -> str:
    """Trim whitespace, strip enclosing quotes, cap at _MAX_SUBJECT_LEN."""
    out = subject.strip()
    if out.startswith(('"', "'")) and out.endswith(('"', "'")) and len(out) >= 2:
        out = out[1:-1].strip()
    # Collapse internal newlines to spaces — subject is a one-liner.
    out = " ".join(out.split())
    if len(out) > _MAX_SUBJECT_LEN:
        out = out[: _MAX_SUBJECT_LEN - 3].rstrip() + "..."
    return out


async def suggest_subject(content: str) -> Optional[str]:
    """Ask the configured Ollama model for a one-line subject.

    Returns ``None`` when:

    * ``AGENT_MCP_SUBJECT_MODEL`` is unset.
    * The ``openai`` package is unavailable (graceful import guard).
    * The HTTP call fails for any reason.
    * The model returns an empty / whitespace-only completion.

    Returns the (trimmed, length-capped) subject string otherwise.
    """
    model = os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip()
    if not model:
        return None

    try:
        import openai  # local import so the module loads on hosts w/o the dep
    except ImportError:  # pragma: no cover — openai is a hard dep, defensive
        logger.debug("suggest_subject: openai package not importable")
        return None

    base_url = os.environ.get("AGENT_MCP_LLM_BASE_URL", _DEFAULT_BASE_URL).strip()
    if not base_url:
        base_url = _DEFAULT_BASE_URL

    # `api_key` is required by the SDK but Ollama ignores it. Use a
    # sentinel that obviously isn't a real OpenAI key.
    try:
        client = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            max_tokens=32,
            temperature=0.2,
        )
    except Exception as e:  # pragma: no cover — covered via unit test mocking
        logger.debug(
            "suggest_subject: %s call failed (base=%s, model=%s): %s",
            type(e).__name__, base_url, model, e,
        )
        return None

    try:
        raw = response.choices[0].message.content or ""
    except (AttributeError, IndexError):  # pragma: no cover — defensive
        logger.debug(
            "suggest_subject: unexpected response shape: %r", response
        )
        return None

    out = _truncate(raw)
    if not out:
        return None
    return out
