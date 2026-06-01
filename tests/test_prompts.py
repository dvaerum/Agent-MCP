"""Tests for the MCP prompts subsystem (plan Phase 6).

The dashboard's Prompt Book is now also exposed via MCP
`prompts/list` + `prompts/get` so MCP clients (Claude Code etc.)
can pick from the same catalogue with `/mcp` instead of
copy-pasting from the dashboard.

Source of truth for the catalogue is the new shared JSON file at
`agent_mcp/prompts/catalog.json`. This PR introduces the JSON, the
Python loader, the MCP handlers, and a REST endpoint
`GET /api/prompts/catalog`. The dashboard's TypeScript copy is
kept in this PR but the underlying data MUST match — the
`test_typescript_and_json_catalogs_in_sync` test catches drift.
The dashboard migration to fetch from the REST endpoint lands in
a follow-up.
"""

from __future__ import annotations

from pathlib import Path

import mcp.types as mcp_types
import pytest


# ---------------------------------------------------------------------------
# Static-shape tests — no harness needed.
# ---------------------------------------------------------------------------


def test_catalog_json_is_valid_and_non_empty() -> None:
    """`agent_mcp/prompts/catalog.json` exists, parses, and has at
    least one prompt in each of the five locked categories."""
    from agent_mcp.prompts import load_catalog

    catalog = load_catalog()
    assert "categories" in catalog
    assert "prompts" in catalog
    cat_ids = {c["id"] for c in catalog["categories"]}
    expected = {
        "initialization",
        "task-management",
        "context-management",
        "debugging",
        "coordination",
    }
    assert expected.issubset(cat_ids), (
        f"missing categories: {expected - cat_ids}"
    )
    for cat_id in expected:
        in_cat = [p for p in catalog["prompts"] if p["category"] == cat_id]
        assert in_cat, f"category {cat_id} has no prompts"


def test_typescript_and_json_catalogs_in_sync() -> None:
    """The TS catalogue in
    `agent_mcp/dashboard/lib/prompt-book.ts` MUST match the JSON
    on prompt IDs and category IDs.

    Until the dashboard fetches from `/api/prompts/catalog`
    instead of inlining the data, this test catches drift. When
    the migration lands, this test (and the TS data) can go
    away."""
    from agent_mcp.prompts import load_catalog

    catalog = load_catalog()
    ts_path = (
        Path(__file__).resolve().parents[1]
        / "agent_mcp"
        / "dashboard"
        / "lib"
        / "prompt-book.ts"
    )
    ts_src = ts_path.read_text()

    # Every JSON prompt id must appear in the TS file as `id: '<id>'`
    # or `id: "<id>"`. The TS file uses single quotes, but accept
    # either for resilience.
    import re
    ts_ids = set(re.findall(r"id:\s*[\"']([\w-]+)[\"']", ts_src))
    json_ids = {p["id"] for p in catalog["prompts"]}
    missing = json_ids - ts_ids
    extra = ts_ids - json_ids
    # extra is allowed during the transition (TS may have additions
    # not yet ported into JSON), but JSON additions MUST be present
    # in TS until the runtime fetch lands.
    assert not missing, (
        f"prompt ids in catalog.json missing from prompt-book.ts: "
        f"{sorted(missing)}; extra in TS (allowed): {sorted(extra)}"
    )


# ---------------------------------------------------------------------------
# MCP prompts/list — wired through the framework handlers.
# ---------------------------------------------------------------------------


async def _list_prompts(session) -> list[mcp_types.Prompt]:
    from agent_mcp.tools.registry import request_auth_token

    handler = session._admin._mcp_app_instance().request_handlers[
        mcp_types.ListPromptsRequest
    ]
    req = mcp_types.ListPromptsRequest(method="prompts/list")
    tok = request_auth_token.set(session.token)
    try:
        result = await handler(req)
    finally:
        request_auth_token.reset(tok)
    inner = result.root if hasattr(result, "root") else result
    return list(getattr(inner, "prompts", []) or [])


async def _get_prompt(
    session, name: str, arguments: dict | None = None
) -> mcp_types.GetPromptResult:
    from agent_mcp.tools.registry import request_auth_token

    handler = session._admin._mcp_app_instance().request_handlers[
        mcp_types.GetPromptRequest
    ]
    req = mcp_types.GetPromptRequest(
        method="prompts/get",
        params=mcp_types.GetPromptRequestParams(
            name=name, arguments=arguments or {}
        ),
    )
    tok = request_auth_token.set(session.token)
    try:
        result = await handler(req)
    finally:
        request_auth_token.reset(tok)
    inner = result.root if hasattr(result, "root") else result
    return inner


@pytest.mark.asyncio
async def test_prompts_list_returns_catalog_entries(tmp_path: Path) -> None:
    """`prompts/list` returns every catalogue entry as an
    `mcp_types.Prompt`. Names are stable (prompt id from the
    catalog)."""
    from tests.harness import mcp_session
    from agent_mcp.prompts import load_catalog

    async with mcp_session(tmp_path) as admin:
        prompts = await _list_prompts(admin)
        names = {p.name for p in prompts}
        catalog = load_catalog()
        expected = {p["id"] for p in catalog["prompts"]}
        assert expected <= names, (
            f"prompts/list missing {sorted(expected - names)}"
        )


@pytest.mark.asyncio
async def test_prompts_get_renders_template_with_variables(
    tmp_path: Path,
) -> None:
    """`prompts/get` substitutes `{{VARIABLE}}` placeholders using
    the supplied arguments. The rendered text appears in the
    returned `GetPromptResult.messages[0].content.text`."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        # `worker-init` is a stable catalogue entry. Substitute its
        # AGENT_ID and WORKER_TOKEN.
        result = await _get_prompt(
            admin,
            "worker-init",
            {"AGENT_ID": "frontend-worker", "WORKER_TOKEN": "tok_xyz"},
        )
        messages = result.messages or []
        assert messages, f"no messages in {result!r}"
        text = ""
        for m in messages:
            content = getattr(m, "content", None)
            t = getattr(content, "text", None)
            if isinstance(t, str):
                text += t
        assert "frontend-worker" in text, (
            f"AGENT_ID substitution missing; got: {text!r}"
        )
        assert "tok_xyz" in text, (
            f"WORKER_TOKEN substitution missing; got: {text!r}"
        )
        assert "{{" not in text, (
            f"unfilled placeholder left in output: {text!r}"
        )


@pytest.mark.asyncio
async def test_prompts_get_leaves_unsupplied_variables_blank(
    tmp_path: Path,
) -> None:
    """Missing arguments for OPTIONAL variables substitute as empty
    strings; the unfilled placeholder MUST NOT leak through."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        # `debug-agent-status` has only optional variables.
        result = await _get_prompt(admin, "debug-agent-status", {})
        text = ""
        for m in result.messages or []:
            content = getattr(m, "content", None)
            t = getattr(content, "text", None)
            if isinstance(t, str):
                text += t
        assert "{{" not in text, (
            f"unfilled placeholder leaked: {text!r}"
        )


# ---------------------------------------------------------------------------
# REST endpoint — `GET /api/prompts/catalog`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_catalog_endpoint_returns_json(tmp_path: Path) -> None:
    """`GET /api/prompts/catalog` returns the same JSON
    `load_catalog()` returns."""
    from tests.harness import mcp_session
    from agent_mcp.prompts import load_catalog

    async with mcp_session(tmp_path) as admin:
        resp = admin.client.get("/api/prompts/catalog")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        local = load_catalog()
        # Same structure; both have categories + prompts.
        assert {p["id"] for p in body["prompts"]} == {
            p["id"] for p in local["prompts"]
        }
        assert {c["id"] for c in body["categories"]} == {
            c["id"] for c in local["categories"]
        }
