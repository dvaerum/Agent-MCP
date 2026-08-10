"""ADR-0017 (Wave 12 PR B): the RAG side-channel returns memory rows in FULL.

This suite pinned the RAG secret-redaction seam (secret-keyed rows dropped
at index + query time). Wave 12 PR B removes content-based secret
detection: project_context is shared project knowledge, returned AS-IS
through ``ask_project_rag``. Protection is by authorization — RAG is
per-project and (for tasks) ownership-scoped — not by guessing content.

What SURVIVES and is still exercised here: the store separation from
ADR-0016. ``config_*`` keys live in the operator-only ``project_settings``
store, which the RAG NEVER scans — so a ``config_*`` secret genuinely
never enters the RAG context. Knowledge keys in ``project_context`` are
now returned verbatim (the former "dropped" assertions are inverted).

These tests drive ``query_rag_system`` / ``query_rag_system_with_model``
directly with a captured LLM client so we can assert on the exact context
text handed to the model.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from tests.harness import mcp_session, seed_config_setting_as_sysadmin

pytestmark = pytest.mark.asyncio


_SECRET_VALUE = "SENTINEL-SECRET-VALUE-9f3a"
_SETTINGS_SECRET = "SETTINGS-STORE-SECRET-1a2b"
_PUBLIC_VALUE = "public-readme-info"


class _CapturingClient:
    """Stand-in completion client that records the messages it is asked
    to synthesise over, so a test can inspect the assembled RAG
    context that reached the model."""

    provider = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages, temperature: float = 0.4) -> str:
        self.messages = messages
        return "SYNTHESISED-ANSWER"


def _user_content(cap: _CapturingClient) -> str:
    assert cap.messages is not None, (
        "LLM was never invoked — the assembled context was empty, so "
        "this test cannot prove the row was returned (vs. simply absent)."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


def _seed(admin, *, key: str, value: str) -> None:
    # Wave 11 (ADR-0016): config_* keys live in the project_settings
    # store (which the RAG never scans) — seed them there as a sysadmin
    # would; knowledge keys flow through the REST memories seam.
    if key.lower().startswith("config_"):
        seed_config_setting_as_sysadmin(key, value)
        return
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


class _StubEmbedder:
    """Deterministic stand-in for the embedding seam so the vector-search
    path resolves a query vector without touching the network."""

    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


def _wire_capture(monkeypatch, *, vss: bool = False) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


# ── store separation: a config_* settings secret never enters the RAG ─


async def test_settings_store_secret_never_enters_rag(
    tmp_path, monkeypatch
) -> None:
    """ADR-0016 store separation SURVIVES: a ``config_*`` secret lives in
    the non-RAG project_settings store, so it never reaches the LLM — not
    because the RAG scans content, but because it isn't in the corpus."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_service_secret", value=_SETTINGS_SECRET)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("tell me about the project tokens")

        user = _user_content(cap)
        assert _SETTINGS_SECRET not in user
        assert "config_service_secret" not in user
        # Knowledge content flows through.
        assert _PUBLIC_VALUE in user
        assert "project_readme" in user


# ── knowledge rows in project_context are returned in FULL ───────────


async def test_advanced_live_context_returns_knowledge_secret_named_row(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        # config_* → settings store (never RAG-scanned); openai_api_key is
        # a KNOWLEDGE key in project_context → returned AS-IS (ADR-0017).
        _seed(admin, key="config_service_secret", value=_SETTINGS_SECRET)
        _seed(admin, key="openai_api_key", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model("analyse task placement")

        user = _user_content(cap)
        # The knowledge row (key + value) is returned in full.
        assert _SECRET_VALUE in user
        assert "openai_api_key" in user
        # The settings-store secret is still absent (store separation).
        assert _SETTINGS_SECRET not in user
        assert "config_service_secret" not in user
        assert _PUBLIC_VALUE in user


async def test_retrieved_context_chunk_returned_in_full(
    tmp_path, monkeypatch
) -> None:
    """A retrieved vector chunk is returned AS-IS — no content-based drop
    (ADR-0017)."""
    async with mcp_session(tmp_path):
        cap = _wire_capture(monkeypatch, vss=True)

        fake_results = [
            {
                "chunk_text": (
                    f"Context Key: api_bearer_token\nValue: {_SECRET_VALUE}"
                ),
                "source_type": "context",
                "source_ref": "api_bearer_token",
                "metadata": {},
                "distance": 0.1,
            },
            {
                "chunk_text": "def hello(): return 'world'",
                "source_type": "code",
                "source_ref": "app/util.py",
                "metadata": {"language": "python"},
                "distance": 0.2,
            },
        ]

        from agent_mcp.repositories import get_rag_repo

        monkeypatch.setattr(
            get_rag_repo(), "search_similar", lambda **kw: fake_results
        )

        await query_rag_system("what tokens does the project use?")

        user = _user_content(cap)
        assert _SECRET_VALUE in user
        assert "api_bearer_token" in user
        assert "def hello()" in user
