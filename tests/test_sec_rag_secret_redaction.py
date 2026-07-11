"""Security: the RAG side-channel must not leak project_context secrets.

FINDING (owner-authorized security review, 2026-07, HIGH, confirmed):
``ask_project_rag`` is callable by any worker (``agent_bearer``).
``query_rag_system`` (features/rag/query.py) previously dumped
project_context Key/Value pairs into the LLM context with NO secret
redaction, through three paths:

  1. ``fetch_recent_context`` — the "live context" window.
  2. the advanced ``SELECT context_key, value ...`` in
     ``query_rag_system_with_model`` (task-placement analysis).
  3. retrieved vector chunks whose ``source_type == "context"`` (a
     secret that was embedded into the index at index time).

``view_project_context`` already redacts secret-keyed rows for
non-admin callers via ``_SECRET_KEY_RE``; the RAG surface bypassed
that gate. A worker could ask ``ask_project_rag("what is
config_aoe_bearer_token?")`` and the LLM would echo the secret.

Fix (single source of truth): a shared ``is_secret_key`` helper in
``tools/project_context_tools.py`` (reusing ``_SECRET_KEY_RE``) is
imported by the RAG index + query paths. Secret-keyed rows are
skipped at index time AND dropped at every query-time
context-assembly site (defense against a stale index that already
embedded a secret).

These tests drive ``query_rag_system`` / ``query_rag_system_with_model``
directly with a captured LLM client so we can assert on the exact
context text handed to the model.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


_SECRET_VALUE = "SENTINEL-SECRET-VALUE-9f3a"
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
        "this test cannot prove the secret was filtered (vs. simply "
        "absent). Seed data / patches are wrong."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


def _seed(admin, *, key: str, value: str) -> None:
    r = admin.client.post(
        "/api/memories",
        json={
            "token": admin.admin_token,
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
    # Route the query embedding through a deterministic stub seam so the
    # assembly + filter code runs without a live embedding endpoint.
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


# ── is_secret_key helper (single source of truth) ────────────────────


async def test_is_secret_key_matches_config_token_family() -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert is_secret_key("config_aoe_bearer_token")
    assert is_secret_key("config_openai_secret")
    assert is_secret_key("config_db_password")
    assert is_secret_key("config_stripe_api_key")
    assert is_secret_key("config_signing_private_key")
    # Non-secret keys must not match (no over-filtering).
    assert not is_secret_key("project_readme")
    assert not is_secret_key("app.settings.theme")
    assert not is_secret_key("")


# ── (1) live-context path in query_rag_system ────────────────────────


async def test_live_context_drops_secret_rows(tmp_path, monkeypatch) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_aoe_bearer_token", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("tell me about the project tokens")

        user = _user_content(cap)
        assert _SECRET_VALUE not in user, (
            "secret VALUE leaked into RAG live-context handed to the LLM"
        )
        assert "config_aoe_bearer_token" not in user, (
            "secret KEY leaked into RAG live-context handed to the LLM"
        )
        # Non-secret context must still flow through (no over-filtering).
        assert _PUBLIC_VALUE in user
        assert "project_readme" in user


# ── (2) advanced SELECT path in query_rag_system_with_model ──────────


async def test_advanced_live_context_drops_secret_rows(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_service_secret", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model("analyse task placement")

        user = _user_content(cap)
        assert _SECRET_VALUE not in user, (
            "secret VALUE leaked into advanced RAG live-context"
        )
        assert "config_service_secret" not in user
        assert _PUBLIC_VALUE in user


# ── (3) retrieved-chunk path (defense against a stale index) ─────────


async def test_retrieved_context_chunk_secret_dropped(
    tmp_path, monkeypatch
) -> None:
    """Even if a secret was already embedded into the vector index
    (source_type == 'context'), the retrieval loop must drop it before
    it reaches the LLM. Non-secret chunks survive."""
    async with mcp_session(tmp_path):
        cap = _wire_capture(monkeypatch, vss=True)

        fake_results = [
            {
                "chunk_text": (
                    f"Context Key: config_aoe_bearer_token\n"
                    f"Value: {_SECRET_VALUE}"
                ),
                "source_type": "context",
                "source_ref": "config_aoe_bearer_token",
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
        # Embedding call: the seam is already stubbed by _wire_capture
        # (_StubEmbedder) so the vector-search path runs without network.

        await query_rag_system("what tokens does the project use?")

        user = _user_content(cap)
        assert _SECRET_VALUE not in user, (
            "secret embedded as a context chunk leaked at retrieval time"
        )
        assert "config_aoe_bearer_token" not in user
        # The non-secret code chunk must survive.
        assert "def hello()" in user


# ── (4) index-time skip (don't embed secrets in the first place) ─────


async def test_indexer_skips_secret_context_rows() -> None:
    """The context-scan in the periodic indexer must apply is_secret_key
    so secret rows are never embedded. The scan lives inside the
    NoReturn ``run_rag_indexing_periodically`` loop (not unit-callable),
    so guard the control at the module level: the retrieval-time filter
    (tested above) is the load-bearing defense; this guards the
    index-time optimisation against a silent refactor drop."""
    import inspect

    from agent_mcp.features.rag import indexing as indexing_mod

    src = inspect.getsource(indexing_mod.run_rag_indexing_periodically)
    assert "is_secret_key" in src, (
        "index-time secret-row skip was removed from the context scan"
    )
