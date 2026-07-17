"""Worker-msg: ``ask_project_rag`` must not dress a RAG *failure* up as a
successful answer.

BUG (worker-facing response classification):
``query_rag_system`` SWALLOWS provider/DB/config failures into a RETURNED
error-prose string (e.g. ``"Error: RAG provider unavailable"``) rather
than raising. ``ask_project_rag`` used to wrap that string as
``Ok(data={"answer": text}, message=text)`` REGARDLESS of success — so a
genuine outage reached the worker as a SUCCESS envelope whose text merely
started with "Error:". The worker then either treated the outage prose as
a factual answer or filed a false bug report.

FIX: ``ask_project_rag`` matches the returned string against
``query.RAG_ERROR_SENTINELS`` and surfaces a ``Failed`` ToolResult (an
error, ``isError=True``, HTTP 500) with a static, category-only message
(SD-R9-1: no provider names / URLs / exception text). The GENUINE
"no relevant information found" answer is NOT a sentinel — an empty
knowledge base is a successful query, so it stays ``Ok``.

Cases pinned here:
  (a) each simulated provider/DB/config error arm -> ``Failed`` (not
      ``Ok``) with a generic message that leaks no exception detail;
  (b) a genuine empty result -> still ``Ok`` "No relevant information";
  (c) a normal synthesised answer -> ``Ok`` carrying the real text.
"""

from __future__ import annotations

import sqlite3

import httpx
import openai
import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Failed, Ok
from agent_mcp.external.completion_service import CompletionConfigError
from agent_mcp.features.rag import query as query_mod
from agent_mcp.tools.rag_tools import ask_project_rag_tool_impl
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _worker_principal(agent_id: str = "w1") -> Principal:
    return make_principal(
        kind="agent_bearer",
        agent_id=agent_id,
        agent_role="worker",
        source_token="tok",
    )


def _openai_error(detail: str) -> openai.APIError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIError(detail, req, body=None)


async def _ask(query: str = "what is the status?"):
    return await ask_project_rag_tool_impl(
        {"query": query}, principal=_worker_principal()
    )


def _assert_generic_failed(result, *leaks: str) -> None:
    """A provider/DB/config failure must surface as ``Failed`` with a
    non-empty, category-only message that leaks no exception detail."""
    assert isinstance(result, Failed), f"expected Failed, got {result!r}"
    msg = result.message or ""
    assert msg.strip(), "Failed message must not be empty"
    for leak in leaks:
        assert leak not in msg, (
            f"exception detail leaked into the worker-facing message: "
            f"{leak!r} found in {msg!r}"
        )
    # It still reads as a transient-unavailable signal, not a fake answer.
    assert "unavailable" in msg.lower()


# ── (a) provider / DB / config error arms -> Failed ──────────────────


async def test_provider_unavailable_surfaces_failed(monkeypatch) -> None:
    """openai.APIError arm -> ``Failed`` (was a false ``Ok`` "Error: …")."""
    leak = "https://api.openai.com/v1 secret-body-sk-9f3a"
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
    monkeypatch.setattr(
        query_mod,
        "get_db_connection",
        lambda: (_ for _ in ()).throw(_openai_error(leak)),
    )
    result = await _ask()
    _assert_generic_failed(result, leak, "api.openai.com")


async def test_db_error_surfaces_failed(monkeypatch) -> None:
    """sqlite3.Error arm -> ``Failed``."""
    leak = "no such column: rag_chunks.secret"
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
    monkeypatch.setattr(
        query_mod,
        "get_db_connection",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError(leak)),
    )
    result = await _ask()
    _assert_generic_failed(result, leak, "rag_chunks")


async def test_unexpected_error_surfaces_failed(monkeypatch) -> None:
    """Generic Exception arm -> ``Failed``."""
    leak = "/var/lib/agentmcp/secret-db/project.sqlite"
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
    monkeypatch.setattr(
        query_mod,
        "get_db_connection",
        lambda: (_ for _ in ()).throw(OSError(leak)),
    )
    result = await _ask()
    _assert_generic_failed(result, leak, "/var/lib/agentmcp")


async def test_completion_not_configured_surfaces_failed(
    tmp_path, monkeypatch
) -> None:
    """CompletionConfigError arm -> ``Failed``.

    Seed a live-context row so ``context_parts`` is non-empty and the
    pipeline reaches ``completion_client()``, which we make raise.
    """
    leak = "OPENAI_MODEL unset; internal-config-path=/etc/agentmcp/x"
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": "project_readme",
                "context_value": "public info the RAG can synthesise over",
            },
        )
        assert r.status_code == 200, r.text

        monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)
        monkeypatch.setattr(
            query_mod,
            "completion_client",
            lambda: (_ for _ in ()).throw(CompletionConfigError(leak)),
        )

        result = await _ask("tell me about project_readme")
    _assert_generic_failed(result, leak, "OPENAI_MODEL", "/etc/agentmcp")


# ── (b) genuine empty result -> still Ok ─────────────────────────────


async def test_empty_result_stays_ok(tmp_path, monkeypatch) -> None:
    """An empty knowledge base is a SUCCESS, not a failure -> ``Ok`` with
    the "No relevant information" prose."""
    async with mcp_session(tmp_path) as admin:  # noqa: F841
        monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        result = await _ask("zzqqxx nomatchtoken")

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    answer = (result.data or {}).get("answer", "")
    assert "No relevant information" in answer
    assert result.message == answer


# ── (c) normal synthesised answer -> Ok ──────────────────────────────


async def test_normal_answer_stays_ok(tmp_path, monkeypatch) -> None:
    """A real synthesised answer surfaces as ``Ok`` carrying the text."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": "project_readme",
                "context_value": "public info the RAG can synthesise over",
            },
        )
        assert r.status_code == 200, r.text

        monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        class _Client:
            provider = "mock"
            model = "mock"

            async def chat(self, messages, temperature: float = 0.4) -> str:
                return "THE-REAL-SYNTHESISED-ANSWER"

        monkeypatch.setattr(query_mod, "completion_client", lambda: _Client())

        result = await _ask("tell me about project_readme")

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert result.data == {"answer": "THE-REAL-SYNTHESISED-ANSWER"}
    assert result.message == "THE-REAL-SYNTHESISED-ANSWER"
