"""Input-overflow guard for the subject helper.

``suggest_subject`` feeds a message body to a local model to produce a
one-line subject. The model runs on a fixed per-slot context window
(llama-cpp ``-c / -np``); an oversized body would overflow that window
and the completion call would 400 with ``exceed_context_size_error``.

The helper caps its OUTPUT (``max_tokens=32`` + an 80-char subject
ceiling) but must ALSO cap its INPUT — a subject only ever needs the
opening of a message. This module pins that guarantee: no matter how
large the body, the content handed to the model is head-truncated to
``_MAX_INPUT_CHARS`` so the call can never overflow the window.

Sibling of the RAG assembler's ``AGENT_MCP_MAX_CONTEXT_TOKENS`` budget
(``features/rag/query.py::_append_within_budget``): together they bound
every variable-size local-model input path.

A CHARACTER cap (not word/token count) is deliberate — it holds as a
true ceiling even for space-less input (a long base64 blob is a single
"word" but thousands of tokens).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _fake_openai_capturing(store: dict) -> MagicMock:
    """An ``openai.AsyncOpenAI`` stand-in whose ``chat.completions.create``
    records the ``messages`` it was called with and returns a fixed
    subject."""
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = "A Concise Subject"
    resp.choices = [choice]

    async def _create(**kwargs):
        store["messages"] = kwargs.get("messages")
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


async def test_oversized_body_is_head_truncated(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    import openai

    from agent_mcp.features import message_suggestions

    store: dict = {}
    monkeypatch.setattr(
        openai, "AsyncOpenAI", lambda **kw: _fake_openai_capturing(store)
    )

    # A body far larger than any plausible context window.
    huge = "word " * 100_000  # ~500 KB
    out = await message_suggestions.suggest_subject(huge)

    assert out == "A Concise Subject"

    user_msg = next(m for m in store["messages"] if m["role"] == "user")
    assert len(user_msg["content"]) <= message_suggestions._MAX_INPUT_CHARS, (
        f"input not truncated: {len(user_msg['content'])} chars "
        f"> cap {message_suggestions._MAX_INPUT_CHARS}"
    )
    # Head-truncation: keep the OPENING of the message.
    assert huge.startswith(user_msg["content"])


async def test_spaceless_blob_is_bounded(monkeypatch) -> None:
    """A single giant space-less token would defeat any word-count cap;
    the character cap must still bound it."""
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    import openai

    from agent_mcp.features import message_suggestions

    store: dict = {}
    monkeypatch.setattr(
        openai, "AsyncOpenAI", lambda **kw: _fake_openai_capturing(store)
    )

    blob = "A" * 200_000  # one "word", ~200 KB
    await message_suggestions.suggest_subject(blob)

    user_msg = next(m for m in store["messages"] if m["role"] == "user")
    assert len(user_msg["content"]) <= message_suggestions._MAX_INPUT_CHARS


async def test_small_body_is_passed_through_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MCP_SUBJECT_MODEL", "qwen2.5:3b-instruct")

    import openai

    from agent_mcp.features import message_suggestions

    store: dict = {}
    monkeypatch.setattr(
        openai, "AsyncOpenAI", lambda **kw: _fake_openai_capturing(store)
    )

    body = "Please review the deploy plan for the washing-brothers backend."
    await message_suggestions.suggest_subject(body)

    user_msg = next(m for m in store["messages"] if m["role"] == "user")
    assert user_msg["content"] == body
