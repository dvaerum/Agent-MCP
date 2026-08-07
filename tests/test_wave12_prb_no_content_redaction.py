"""Wave 12 PR B — no content-based secret redaction anywhere (ADR-0017).

Headline behaviour: a project_context (memory) row whose KEY is
secret-shaped (``db_password``) and whose VALUE looks like a live
credential (``sk_live_…``) is returned IN FULL — never ``[redacted]``,
never dropped — through every project_context read surface:

  1. the MCP ``view_project_context`` tool, for a WORKER (non-admin) AND
     an operator;
  2. the ``/api/context-data`` + ``/api/all-data`` dashboard REST reads,
     for a non-confirmed (forwarding-header) operator;
  3. the worker-reachable RAG live-context path (``query_rag_system``).

memory is SHARED PROJECT CONTENT; protection is by AUTHORIZATION (who may
read the project), not by guessing content. Real secrets belong in sops
refs or the operator-only, non-RAG project_settings store — which keeps
its own settings-store redaction (out of scope here).

This is the RED→GREEN pin for the deletion of ``is_secret_key`` /
``_value_has_embedded_secret`` / the RAG secret drops / the composition
redaction: on the pre-deletion code every ``in`` assertion below was a
``not in`` (the value was redacted), so this file fails RED there and
passes GREEN once the content-scanning net is removed.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import query_rag_system
from agent_mcp.tools.project_context_tools import (
    view_project_context_tool_impl,
)
from tests.harness import make_principal, mcp_session

# A secret-NAMED key AND a credential-SHAPED value — the two shapes the
# old detector (key-name vocab + embedded-value scanner) redacted. The
# value is a synthetic high-entropy sentinel (NOT a real provider format,
# so it doesn't trip upstream secret scanners) — under ADR-0017 the shape
# no longer matters: content is returned AS-IS regardless.
_SECRET_KEY = "db_password"
_SECRET_VALUE = "ZZk-demo-wave12prbSENTINEL0123456789abcd"
_PUBLIC_KEY = "project_readme"
_PUBLIC_VALUE = "public-wave12-info"


def _seed(admin, *, key: str, value: str) -> None:
    """Seed a project_context knowledge row through the live REST seam."""
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _operator_principal() -> object:
    return make_principal(
        kind="operator_session",
        user_id="wave12-op",
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── (1) MCP view_project_context — worker AND operator see it in full ──


@pytest.mark.asyncio
async def test_view_project_context_returns_secret_shaped_row_in_full(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_SECRET_KEY, value=_SECRET_VALUE)
        _seed(admin, key=_PUBLIC_KEY, value=_PUBLIC_VALUE)

        worker = await admin.create_worker("wave12-worker")

        # Worker (non-admin) sees the secret-named key AND its value.
        worker_text = (await worker.call("view_project_context", {}))[0].text
        assert _SECRET_KEY in worker_text
        assert _SECRET_VALUE in worker_text
        assert _PUBLIC_VALUE in worker_text

        # Operator sees the same — no redaction on either tier.
        op_result = await view_project_context_tool_impl(
            {}, principal=_operator_principal()
        )
        op_text = op_result.message or ""
        assert _SECRET_KEY in op_text
        assert _SECRET_VALUE in op_text


# ── (2) dashboard REST reads — non-confirmed operator sees it in full ──


@pytest.mark.asyncio
async def test_context_data_returns_secret_shaped_row_in_full(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_SECRET_KEY, value=_SECRET_VALUE)

        # Forwarding-header operator = NOT confirmed tier; pre-ADR-0017
        # this path returned ``[redacted]``.
        r = admin.get("/api/context-data")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE in r.text
        assert "[redacted]" not in r.text

        r2 = admin.get("/api/all-data")
        assert r2.status_code == 200, r2.text
        assert _SECRET_VALUE in r2.text


# ── (3) RAG live-context — worker-reachable path echoes it in full ─────


@pytest.mark.asyncio
async def test_rag_live_context_returns_secret_shaped_row_in_full(
    tmp_path, monkeypatch
) -> None:
    class _Cap:
        provider = "mock"
        model = "mock"

        def __init__(self) -> None:
            self.messages = None

        async def chat(self, messages, temperature: float = 0.4) -> str:
            self.messages = messages
            return "ANSWER"

    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_SECRET_KEY, value=_SECRET_VALUE)

        cap = _Cap()
        monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        await query_rag_system("what database credentials does the project use?")

        assert cap.messages is not None, "LLM never invoked"
        user = next(m["content"] for m in cap.messages if m["role"] == "user")
        assert _SECRET_VALUE in user, (
            "ADR-0017: the RAG live-context must return the memory row's "
            "value AS-IS — no content-based secret drop"
        )
