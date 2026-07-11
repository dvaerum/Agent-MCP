"""Round-3 secret-leak hardening (owner-authorized security review).

The round-2 fix (#294) broadened ``is_secret_key`` with a
delimited-segment regex, but a live worker read still recovered a
family of credential keys in cleartext:

  * SD-1 — camelCase secret keys (``clientSecret``, ``accessToken``,
    ``refreshToken``, ``sessionToken``, ``authToken``, ``apiSecret``)
    slipped through because the delimited regex never treated a
    lowerUpper transition as a word boundary; and a set of
    non-delimited / missing-vocab keys (``pwd``, ``seed``,
    ``mnemonic``, ``privkey``, ``sessioncookie``, ``bearertoken``) plus
    connection-string / DSN key names (``database_url``, ``conn_str``,
    ``dsn``, ``connection_string``) were simply not in the vocab.
  * SD-1(c) — defense-in-depth: a secret pasted into the VALUE of a
    benign-named key must be dropped at the ``view_project_context``
    tool boundary too (mirror the index-time value scan).
  * SD-2 — the RAG live-context query paths
    (``query_rag_system`` / ``query_rag_system_with_model``) filtered
    rows by ``is_secret_key`` ONLY, so an embedded secret in the VALUE
    of a non-secret key reached the LLM via ``ask_project_rag``. The
    index path already skips ``is_secret_key(key) OR
    _value_has_embedded_secret(value, desc)`` — mirror it here.

These tests pin the fix WITHOUT over-redacting innocent keys.
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
# A value carrying an embedded credential (OpenAI-style key) that the
# value scanner must flag even under a benign KEY name.
_EMBEDDED_SECRET_VALUE = "deploy step: export KEY=sk-r3abcdef0123456789ABCDEF"


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
        "test cannot prove the secret was filtered vs. simply absent."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


class _StubEmbedder:
    """Deterministic stand-in for the embedding seam so the query path
    resolves a query vector without touching the network."""

    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


def _wire_capture(monkeypatch, *, vss: bool = False) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


# ── SD-1 (a/b): is_secret_key covers camelCase + new vocab ───────────

# camelCase secret keys — the round-2 delimited regex missed these.
_CAMEL_SECRET_KEYS = (
    "clientSecret",
    "accessToken",
    "refreshToken",
    "sessionToken",
    "authToken",
    "apiSecret",
)

# Non-delimited / missing-vocab + connection-string / DSN key names.
_VOCAB_SECRET_KEYS = (
    "pwd",
    "db_pwd",
    "seed",
    "wallet_seed",
    "mnemonic",
    "privkey",
    "sessioncookie",
    "bearertoken",
    "database_url",
    "conn_str",
    "dsn",
    "connection_string",
)

# Innocent keys that merely CONTAIN secret letters — must NOT trip.
_INNOCENT_KEYS = (
    "project_theme",
    "monkey",
    "passenger",
    "author",
    "seedling",
    "homepage_url",
    "readme",
    "app.settings.theme",
    "",
)


@pytest.mark.parametrize("key", _CAMEL_SECRET_KEYS)
def test_is_secret_key_covers_camelcase(key: str) -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert is_secret_key(key), f"{key} (camelCase) should be secret"


@pytest.mark.parametrize("key", _VOCAB_SECRET_KEYS)
def test_is_secret_key_covers_new_vocab(key: str) -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert is_secret_key(key), f"{key} should be secret (new vocab)"


@pytest.mark.parametrize("key", _INNOCENT_KEYS)
def test_is_secret_key_does_not_over_redact_r3(key: str) -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert not is_secret_key(key), f"{key} should NOT be secret"


# ── SD-1 (2): worker view_project_context redacts the new keys ───────


@pytest.mark.asyncio
async def test_worker_view_context_redacts_camel_and_vocab_keys(
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
        for k in (
            "clientSecret",
            "accessToken",
            "db_pwd",
            "wallet_seed",
            "database_url",
        ):
            assert k not in text, f"secret key {k} leaked to worker"
        assert _SECRET_VALUE not in text
        # Non-secret content still visible (no over-filtering).
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


# ── SD-1 (c): value backstop at the tool boundary ────────────────────


@pytest.mark.asyncio
async def test_worker_view_context_drops_embedded_secret_value(
    tmp_path,
) -> None:
    """A secret pasted into the VALUE of a BENIGN-named key must be
    redacted at the view_project_context boundary (mirror the index-time
    value scan). Innocent benign-named keys must survive."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_notes", value=_PUBLIC_VALUE)
        worker = await admin.create_worker("r3-worker-val")

        text = (await worker.call("view_project_context", {}))[0].text
        assert "sk-r3abcdef0123456789ABCDEF" not in text, (
            "embedded secret in a benign key's VALUE leaked at the tool "
            "boundary"
        )
        assert "deploy_notes" not in text
        # Benign key with benign value must remain.
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


# ── SD-2: RAG live-context value scan (both query paths) ─────────────


@pytest.mark.asyncio
async def test_live_context_drops_embedded_secret_value(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we deploy the project?")

        user = _user_content(cap)
        assert "sk-r3abcdef0123456789ABCDEF" not in user, (
            "embedded secret in a benign key's VALUE reached the LLM via "
            "query_rag_system"
        )
        # Non-secret context still flows through.
        assert _PUBLIC_VALUE in user


@pytest.mark.asyncio
async def test_advanced_live_context_drops_embedded_secret_value(
    tmp_path, monkeypatch
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="deploy_notes", value=_EMBEDDED_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model("analyse task placement")

        user = _user_content(cap)
        assert "sk-r3abcdef0123456789ABCDEF" not in user, (
            "embedded secret in a benign key's VALUE reached the LLM via "
            "query_rag_system_with_model"
        )
        assert _PUBLIC_VALUE in user


@pytest.mark.asyncio
async def test_live_context_still_drops_camel_secret_key(
    tmp_path, monkeypatch
) -> None:
    """Regression: the RAG live-context must also drop the newly-covered
    camelCase secret KEY (not just embedded-value secrets)."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="clientSecret", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_rag_system("what client secret do we use?")

        user = _user_content(cap)
        assert _SECRET_VALUE not in user
        assert "clientSecret" not in user
        assert _PUBLIC_VALUE in user
