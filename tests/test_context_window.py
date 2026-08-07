"""Context-window discovery + budget derivation.

The RAG assembler and the subject helper both bound their input to the
chat model's context window. Rather than hardcode that window per
deploy, ``context_window`` discovers it at runtime from the endpoint
(llama-cpp ``GET /props`` → ``n_ctx``), so budgets adapt automatically
to whatever host agent-mcp runs on — including ones with a SMALLER
window. Explicit env overrides always win (override-on-top).

Resolution order for the window:
1. ``AGENT_MCP_MODEL_CONTEXT_WINDOW`` env (explicit override),
2. a cached probe of ``{base_url}/props``,
3. a conservative default (fail-safe when nothing is reachable).
"""

from __future__ import annotations

import pytest

from agent_mcp.external import context_window as cw

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_MODEL_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("AGENT_MCP_MAX_CONTEXT_TOKENS", raising=False)
    cw._WINDOW_CACHE.clear()
    yield
    cw._WINDOW_CACHE.clear()


# ── window discovery ──────────────────────────────────────────────────


async def test_env_override_wins_without_probing(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_MODEL_CONTEXT_WINDOW", "5000")

    async def _boom(_):  # probe must not be called
        raise AssertionError("probe should be skipped when env override set")

    monkeypatch.setattr(cw, "_probe_props", _boom)
    assert await cw.resolve_context_window("http://x/v1") == 5000


async def test_probe_returns_n_ctx(monkeypatch):
    async def _probe(_):
        return 8192

    monkeypatch.setattr(cw, "_probe_props", _probe)
    assert await cw.resolve_context_window("http://llama/v1") == 8192


async def test_probe_failure_returns_none(monkeypatch):
    async def _probe(_):
        return None  # unreachable / bad shape / non-llama-cpp

    monkeypatch.setattr(cw, "_probe_props", _probe)
    assert await cw.resolve_context_window("http://down/v1") is None


async def test_result_is_cached_per_endpoint(monkeypatch):
    calls = {"n": 0}

    async def _probe(_):
        calls["n"] += 1
        return 8192

    monkeypatch.setattr(cw, "_probe_props", _probe)
    await cw.resolve_context_window("http://llama/v1")
    await cw.resolve_context_window("http://llama/v1")
    assert calls["n"] == 1  # second call served from cache


async def test_failure_is_also_cached(monkeypatch):
    """A cloud endpoint without /props must not be re-probed (and eat the
    timeout) on every query."""
    calls = {"n": 0}

    async def _probe(_):
        calls["n"] += 1

    monkeypatch.setattr(cw, "_probe_props", _probe)
    await cw.resolve_context_window("http://cloud/v1")
    await cw.resolve_context_window("http://cloud/v1")
    assert calls["n"] == 1


async def test_none_base_url_returns_none(monkeypatch):
    async def _boom(_):
        raise AssertionError("no probe without a base_url")

    monkeypatch.setattr(cw, "_probe_props", _boom)
    assert await cw.resolve_context_window(None) is None


# ── RAG budget derivation (override-on-top) ───────────────────────────


async def test_max_context_tokens_env_override_wins(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_MAX_CONTEXT_TOKENS", "6000")

    async def _boom(_):
        raise AssertionError("explicit budget override must not probe")

    monkeypatch.setattr(cw, "_probe_props", _boom)
    assert await cw.resolve_max_context_tokens("http://llama/v1") == 6000


def _expected_budget(window: int) -> int:
    ctx = window - cw._ANSWER_RESERVE_TOKENS - cw._PROMPT_OVERHEAD_TOKENS
    return max(cw._MIN_BUDGET, ctx // cw._MAX_TOKENS_PER_WORD)


async def test_max_context_tokens_derived_from_window(monkeypatch):
    async def _probe(_):
        return 8192

    monkeypatch.setattr(cw, "_probe_props", _probe)
    got = await cw.resolve_max_context_tokens("http://llama/v1")
    assert got == _expected_budget(8192)
    assert got >= cw._MIN_BUDGET
    # The derived WORD budget, at the worst-case tokens/word, plus the
    # reserved answer+overhead tokens, must never exceed the window.
    assert (
        got * cw._MAX_TOKENS_PER_WORD
        + cw._ANSWER_RESERVE_TOKENS
        + cw._PROMPT_OVERHEAD_TOKENS
    ) <= 8192


async def test_derived_budget_shrinks_on_small_window(monkeypatch):
    async def _small(_):
        return 4096

    async def _large(_):
        return 8192

    monkeypatch.setattr(cw, "_probe_props", _small)
    cw._WINDOW_CACHE.clear()
    small = await cw.resolve_max_context_tokens("http://small/v1")

    monkeypatch.setattr(cw, "_probe_props", _large)
    cw._WINDOW_CACHE.clear()
    large = await cw.resolve_max_context_tokens("http://large/v1")

    assert small == _expected_budget(4096)
    assert small >= cw._MIN_BUDGET          # never below the floor
    assert small < large                    # smaller window → smaller budget


async def test_derived_budget_never_below_floor(monkeypatch):
    async def _probe(_):
        return 512  # tiny window, window - headroom would go negative

    monkeypatch.setattr(cw, "_probe_props", _probe)
    assert await cw.resolve_max_context_tokens("http://tiny/v1") == cw._MIN_BUDGET


async def test_undiscoverable_window_preserves_unbounded_budget(monkeypatch):
    """Cloud / unreachable endpoint (window None) must keep today's
    unbounded budget — discovery only tightens, never regresses."""
    async def _probe(_):
        return None

    monkeypatch.setattr(cw, "_probe_props", _probe)
    assert (
        await cw.resolve_max_context_tokens("http://cloud/v1")
        == cw._LEGACY_UNBOUNDED_BUDGET
    )
    # ...and with no base_url at all:
    assert await cw.resolve_max_context_tokens(None) == cw._LEGACY_UNBOUNDED_BUDGET


# ── subject input cap (auto-shrink, ceiling-capped) ───────────────────


async def test_subject_cap_ceiling_on_large_window(monkeypatch):
    async def _probe(_):
        return 8192

    monkeypatch.setattr(cw, "_probe_props", _probe)
    # a subject never needs more than the ceiling even on a huge window
    assert await cw.resolve_subject_input_chars("http://llama/v1") == cw._SUBJECT_CHARS_CEILING


async def test_subject_cap_shrinks_on_small_window(monkeypatch):
    async def _probe(_):
        return 1024

    monkeypatch.setattr(cw, "_probe_props", _probe)
    got = await cw.resolve_subject_input_chars("http://small/v1")
    assert got < cw._SUBJECT_CHARS_CEILING  # auto-shrunk below the ceiling
    assert got > 0


async def test_subject_cap_falls_back_to_ceiling_when_undiscoverable(monkeypatch):
    async def _probe(_):
        return None

    monkeypatch.setattr(cw, "_probe_props", _probe)
    assert (
        await cw.resolve_subject_input_chars("http://cloud/v1")
        == cw._SUBJECT_CHARS_CEILING
    )
