"""Security (round 9, SD-R9-1, MED): RAG error-prose must not reflect
raw exception detail to the caller.

FINDING (owner-authorized security review, confirmed):
``ask_project_rag`` is reachable by any WORKER (it is gated only on
the ``rag.query`` capability, which workers hold). It calls
``query_rag_system`` (features/rag/query.py), which SWALLOWS its own
exceptions into a normal-looking ``answer`` string and returns it;
``ask_project_rag`` then wraps that string in ``Ok(message=..., data=
{"answer": ...})``, which renders VERBATIM to the client. The error
arms embedded raw exception text:

  * ``sqlite3.Error`` -> table/column names, SQL, "database disk
    image is malformed"
  * arbitrary ``Exception`` -> filesystem paths (OSError), internals
  * ``openai.APIError`` -> provider endpoint URL / error body
  * ``CompletionConfigError`` -> completion-config detail

This bypassed the render-site genericization the earlier rounds
applied to the ``Failed`` variant. The detail is ALREADY logged
server-side with ``exc_info=True``, so genericizing loses nothing.

Fix: the four error arms return STATIC generic strings (no ``{e}`` /
``str(e)``), keeping the server-side ``logger.*(..., exc_info=True)``.

These tests force each arm to fire and assert the string handed back
to the caller (via ``ask_project_rag`` -> rendered ``Ok``) contains
NONE of the raw exception text, only a generic message — while the
detail WAS logged server-side.
"""

from __future__ import annotations

import logging
import sqlite3

import httpx
import openai
import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from agent_mcp.external.completion_service import CompletionConfigError
from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from agent_mcp.tools.rag_tools import ask_project_rag_tool_impl
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# Sentinels planted inside each forced exception. If any of these
# reaches the caller, the error-prose leaked.
_SQL_LEAK = "no such column: rag_chunks.secret"
_PATH_LEAK = "/var/lib/agentmcp/secret-db/project.sqlite"
_OPENAI_LEAK = "https://api.openai.com/v1/internal secret-body-sk-9f3a"
_CONFIG_LEAK = "OPENAI_MODEL unset; internal-config-path=/etc/agentmcp/x"


def _worker_principal(agent_id: str = "w1") -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="tok",
    )


def _openai_error() -> openai.APIError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIError(_OPENAI_LEAK, req, body=None)


async def _rendered_answer(query: str = "what is the status?") -> str:
    """Drive the real tool seam and return the string the client sees.

    ``ask_project_rag`` wraps the (possibly error-prose) answer in
    ``Ok(message=..., data={"answer": ...})``; both surfaces render
    verbatim, so we assert on the message and confirm data matches.
    """
    result = await ask_project_rag_tool_impl(
        {"query": query}, principal=_worker_principal()
    )
    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    msg = result.message or ""
    assert isinstance(result.data, dict)
    assert result.data.get("answer") == msg
    return msg


def _assert_generic(answer: str, *leaks: str) -> None:
    for leak in leaks:
        assert leak not in answer, (
            f"raw exception detail leaked to caller: {leak!r} found in "
            f"answer {answer!r}"
        )
    # The generic prose still tells the user something went wrong.
    assert answer.strip(), "error answer must not be empty"
    assert any(
        w in answer.lower() for w in ("error", "unavailable", "failed", "unexpected")
    ), f"generic error message expected, got {answer!r}"


# ── sqlite3.Error arm (query.py: except sqlite3.Error) ───────────────


async def test_sql_error_not_reflected_to_caller(monkeypatch, caplog) -> None:
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())

    def _boom() -> object:
        raise sqlite3.OperationalError(_SQL_LEAK)

    monkeypatch.setattr(query_mod, "get_db_connection", _boom)

    with caplog.at_level(logging.ERROR):
        answer = await _rendered_answer()

    _assert_generic(answer, _SQL_LEAK, "rag_chunks")
    # Detail WAS logged server-side (exc_info) — nothing is lost.
    assert any(_SQL_LEAK in r.getMessage() for r in caplog.records), (
        "the SQL detail must still be logged server-side"
    )


# ── generic Exception arm (OSError with a filesystem path) ───────────


async def test_unexpected_oserror_path_not_reflected(monkeypatch, caplog) -> None:
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())

    def _boom() -> object:
        raise OSError(_PATH_LEAK)

    monkeypatch.setattr(query_mod, "get_db_connection", _boom)

    with caplog.at_level(logging.ERROR):
        answer = await _rendered_answer()

    _assert_generic(answer, _PATH_LEAK, "/var/lib/agentmcp")
    assert any(_PATH_LEAK in r.getMessage() for r in caplog.records), (
        "the OSError path must still be logged server-side"
    )


# ── openai.APIError arm ──────────────────────────────────────────────


async def test_openai_error_body_not_reflected(monkeypatch, caplog) -> None:
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())

    def _boom() -> object:
        raise _openai_error()

    monkeypatch.setattr(query_mod, "get_db_connection", _boom)

    with caplog.at_level(logging.ERROR):
        answer = await _rendered_answer()

    _assert_generic(answer, _OPENAI_LEAK, "api.openai.com", "secret-body-sk-9f3a")
    assert any(_OPENAI_LEAK in r.getMessage() for r in caplog.records), (
        "the OpenAI error detail must still be logged server-side"
    )


# ── CompletionConfigError arm (reached at the chat-completion call) ──


async def test_completion_config_error_not_reflected(
    tmp_path, monkeypatch, caplog
) -> None:
    """Force the config-error arm: a real DB with seeded live context
    makes ``context_parts`` non-empty, so the code reaches
    ``completion_client()``, which we make raise CompletionConfigError.
    """
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

        def _raise_cfg() -> object:
            raise CompletionConfigError(_CONFIG_LEAK)

        monkeypatch.setattr(query_mod, "completion_client", _raise_cfg)

        with caplog.at_level(logging.ERROR):
            answer = await _rendered_answer("tell me about project_readme")

    _assert_generic(answer, _CONFIG_LEAK, "OPENAI_MODEL", "/etc/agentmcp")
    assert any(_CONFIG_LEAK in r.getMessage() for r in caplog.records), (
        "the completion-config detail must still be logged server-side"
    )


# ── query_rag_system_with_model (lower exposure, same policy) ────────


async def test_with_model_error_not_reflected(monkeypatch, caplog) -> None:
    monkeypatch.setattr(query_mod, "embedding_client", lambda: object())

    def _boom() -> object:
        raise sqlite3.OperationalError(_SQL_LEAK)

    monkeypatch.setattr(query_mod, "get_db_connection", _boom)

    with caplog.at_level(logging.ERROR):
        answer = await query_rag_system_with_model("anything")

    _assert_generic(answer, _SQL_LEAK, "rag_chunks")
    assert any(_SQL_LEAK in r.getMessage() for r in caplog.records), (
        "the SQL detail must still be logged server-side"
    )


# ── regression: a successful query still returns the real answer ─────


async def test_successful_query_returns_real_answer(tmp_path, monkeypatch) -> None:
    async with mcp_session(tmp_path):
        monkeypatch.setattr(query_mod, "embedding_client", lambda: object())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        class _Client:
            provider = "mock"
            model = "mock"

            async def chat(self, messages, temperature: float = 0.4) -> str:
                return "THE-REAL-SYNTHESISED-ANSWER"

        monkeypatch.setattr(query_mod, "completion_client", lambda: _Client())

        # No live data and no vector search -> the "no relevant
        # information" branch, a legitimate (non-error) answer.
        answer = await query_rag_system("zzqqxx nomatchtoken")
    assert "error" not in answer.lower()
    assert "No relevant information" in answer
