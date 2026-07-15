"""ADR-0017 (Wave 12 PR B): memory is returned in FULL — no content-based
secret redaction.

This suite was the round-2 "secret-redaction hardening" pin. Wave 12 PR B
reverses that decision: heuristic content-scanning is unreliable in both
directions (false positives hid legitimate memory notes; false negatives
gave false confidence), so agent-mcp no longer detects-and-censors secrets
in content. project_context is shared project knowledge, returned AS-IS to
any authorized reader; real secrets belong in sops refs or the
operator-only, non-RAG project_settings store.

The former "value redacts for workers / non-confirmed operators" tests are
inverted here to "value is returned in FULL" — the load-bearing behaviour
change. The pure ``is_secret_key`` / ``_value_has_embedded_secret``
detector-unit tests are deleted (the machinery is gone). The unrelated
``max_results`` clamp coverage is preserved verbatim.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import query_rag_system
from tests.harness import make_principal, mcp_session


_SECRET_VALUE = "SENTINEL-R2-SECRET-4c8e"
_PUBLIC_VALUE = "public-r2-info"


def _seed(admin, *, key: str, value: str) -> None:
    """Seed a project_context knowledge row through the live REST seam."""
    r = admin.client.post(
        "/api/memories",
        json={
            "token": admin.admin_token,
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


# ── worker read surfaces (tool + RAG) return secret-shaped rows in full ─


@pytest.mark.asyncio
async def test_worker_view_context_returns_secret_named_keys(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="db_password", value=_SECRET_VALUE)
        _seed(admin, key="openai_api_key", value=_SECRET_VALUE + "-2")
        _seed(admin, key="project_notes", value=_PUBLIC_VALUE)
        worker = await admin.create_worker("r2-worker")

        text = (await worker.call("view_project_context", {}))[0].text
        # ADR-0017: secret-named knowledge keys + their values return AS-IS.
        assert "db_password" in text
        assert "openai_api_key" in text
        assert _SECRET_VALUE in text
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


@pytest.mark.asyncio
async def test_ask_project_rag_returns_secret_named_keys(
    tmp_path, monkeypatch
) -> None:
    """The worker-reachable RAG live-context returns memory rows AS-IS —
    secret-named keys included."""

    class _Cap:
        provider = "mock"
        model = "mock"

        def __init__(self) -> None:
            self.messages = None

        async def chat(self, messages, temperature: float = 0.4) -> str:
            self.messages = messages
            return "ANSWER"

    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="db_password", value=_SECRET_VALUE)
        _seed(admin, key="openai_api_key", value=_SECRET_VALUE + "-2")
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)

        cap = _Cap()
        monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        await query_rag_system("what api keys does the project use?")

        assert cap.messages is not None, "LLM never invoked"
        user = next(m["content"] for m in cap.messages if m["role"] == "user")
        assert _SECRET_VALUE in user
        assert "db_password" in user
        assert "openai_api_key" in user
        assert _PUBLIC_VALUE in user


# ── dashboard composition endpoints return values in full ────────────


@pytest.mark.asyncio
async def test_all_data_returns_secret_context_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="db_password", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)

        # Non-confirmed operator (forwarding header) — sees the value now.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE in r.text
        assert _PUBLIC_VALUE in r.text

        # Confirmed operator bearer → also present.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_VALUE in r2.text


@pytest.mark.asyncio
async def test_node_details_returns_secret_context_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="db_password", value=_SECRET_VALUE)

        node = "context_db_password"
        r = admin.get(f"/api/node-details?node_id={node}")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE in r.text

        r2 = admin.client.get(
            f"/api/node-details?node_id={node}", headers=_bearer(admin)
        )
        assert r2.status_code == 200, r2.text
        assert _SECRET_VALUE in r2.text


# ── max_results clamp (unrelated; preserved) ─────────────────────────


@pytest.mark.asyncio
async def test_view_context_clamps_negative_max_results(tmp_path) -> None:
    """max_results=-1 would otherwise become SQLite ``LIMIT -1`` (no
    limit → full dump). The tool schema declares ``minimum: 1`` so the
    jsonschema-validating dispatcher rejects it — but that dependency is
    optional. This drives the impl DIRECTLY (the jsonschema-absent path)
    and asserts the in-code clamp caps the result at a single row despite
    three rows existing."""
    from agent_mcp.tools.project_context_tools import (
        view_project_context_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="r2_clamp_a", value="aaa")
        _seed(admin, key="r2_clamp_b", value="bbb")
        _seed(admin, key="r2_clamp_c", value="ccc")

        principal = make_principal(
            kind="agent_bearer",
            user_id="op",
            agent_id="admin",
            sysadmin=True,
            project_name="harness",
            project_role="operator",
            agent_role="manager",
            can_wake_loop=False,
            source_token=None,
        )
        result = await view_project_context_tool_impl(
            {"max_results": -1}, principal=principal
        )
        assert result.data["count"] == 1, (
            "max_results=-1 was not clamped to a single-row LIMIT; got "
            f"count={result.data['count']}"
        )
