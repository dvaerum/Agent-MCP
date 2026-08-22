"""ADR-0017 (Wave 12 PR B): memory rows return in FULL — no content-based
secret redaction.

This was the round-3 "secret-leak hardening" suite (camelCase secret keys,
new vocab, embedded-value scan across the RAG query paths). Wave 12 PR B
removes content-based secret detection entirely: project_context is shared
project knowledge, returned AS-IS; real secrets belong in sops refs or the
operator-only, non-RAG project_settings store.

The pure ``is_secret_key`` detector-unit tests are deleted (the predicate
is gone). The former "worker read / RAG live-context redacts the row"
tests are inverted here to "the row is returned in FULL".
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from tests.harness import mcp_session

# ── shared helpers ───────────────────────────────────────────────────

_SECRET_VALUE = "SENTINEL-R3-SECRET-7d2b"
_PUBLIC_VALUE = "public-r3-info"
# A value carrying an embedded credential (OpenAI-style key) — under
# ADR-0017 it is returned AS-IS, never scanned/dropped.
_EMBEDDED_SECRET = "sk-r3abcdef0123456789ABCDEF"
_EMBEDDED_SECRET_VALUE = f"deploy step: export KEY={_EMBEDDED_SECRET}"


def _seed(admin, *, key: str, value: str) -> None:
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


class _CapturingClient:
    provider = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages, temperature: float = 0.4) -> str:
        self.messages = messages
        return "SYNTHESISED-ANSWER"


def _user_content(cap: _CapturingClient) -> str:
    assert cap.messages is not None, (
        "LLM was never invoked — assembled context was empty, so the "
        "test cannot prove the row was returned vs. simply absent."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


class _StubEmbedder:
    """Deterministic stand-in for the embedding seam so the query path
    resolves a query vector without touching the network."""

    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]

    async def aembed(self, texts):
        # R12-F2: query_rag_system calls the async aembed(), never the
        # sync embed() — this stub must answer both.
        return [[0.0] * 8 for _ in texts]


def _wire_capture(monkeypatch, *, vss: bool = False) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


# ── worker view_project_context returns the rows in full ─────────────


@pytest.mark.asyncio
async def test_worker_view_context_returns_camel_and_vocab_keys(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="clientSecret", value=_SECRET_VALUE)
        _seed(admin, key="accessToken", value=_SECRET_VALUE + "-2")
        _seed(admin, key="db_pwd", value=_SECRET_VALUE + "-3")
        _seed(admin, key="wallet_seed", value=_SECRET_VALUE + "-4")
        _seed(admin, key="database_url", value=_SECRET_VALUE + "-5")
        _seed(admin, key="project_notes", value=_PUBLIC_VALUE)
        worker = await admin.create_worker("r3-worker")

        text = (await worker.call("view_project_context", {}))[0].text
        # ADR-0017: every key + value is returned AS-IS to the worker.
        for k in (
            "clientSecret",
            "accessToken",
            "db_pwd",
            "wallet_seed",
            "database_url",
        ):
            assert k in text, f"key {k} must be returned in full"
        assert _SECRET_VALUE in text
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


@pytest.mark.asyncio
async def test_worker_view_context_returns_embedded_secret_value(
    tmp_path,
) -> None:
    """A credential-shaped value under a benign key is returned AS-IS at
    the view_project_context boundary — no content scan."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_notes", value=_PUBLIC_VALUE)
        worker = await admin.create_worker("r3-worker-val")

        text = (await worker.call("view_project_context", {}))[0].text
        assert _EMBEDDED_SECRET in text
        assert "deploy_notes" in text
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


# ── RAG live-context returns the rows in full (both query paths) ─────


@pytest.mark.asyncio
async def test_live_context_returns_embedded_secret_value(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we deploy the project?")

        user = _user_content(cap)
        assert _EMBEDDED_SECRET in user
        assert _PUBLIC_VALUE in user


@pytest.mark.asyncio
async def test_advanced_live_context_returns_embedded_secret_value(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model("analyse task placement")

        user = _user_content(cap)
        assert _EMBEDDED_SECRET in user
        assert _PUBLIC_VALUE in user


@pytest.mark.asyncio
async def test_live_context_returns_camel_secret_key(
    tmp_path, monkeypatch
) -> None:
    """The RAG live-context returns a camelCase secret-named row AS-IS."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="clientSecret", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("what client secret do we use?")

        user = _user_content(cap)
        assert _SECRET_VALUE in user
        assert "clientSecret" in user
        assert _PUBLIC_VALUE in user
