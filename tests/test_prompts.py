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


# `test_typescript_and_json_catalogs_in_sync` retired in the
# dashboard-prompts-from-rest migration — `prompt-book.ts` no longer
# inlines the catalogue, so there's nothing to drift. The replacement
# regression guard lives in tests/test_dashboard_prompts_from_rest.py
# and asserts the dashboard reads via the zustand promptsCatalog
# slice instead.


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
    """Missing arguments substitute as empty strings; the unfilled
    placeholder MUST NOT leak through.

    Uses `debug-task-flow` (the smallest remaining catalog entry —
    one required variable). The debug-agent-status fixture this test
    used to lean on was deleted in the prompt-book cleanup PR
    (duplicated the Agents dashboard page)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        result = await _get_prompt(admin, "debug-task-flow", {})
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
# Regression guards — added in the prompt-book cleanup PR.
#
# 1. Two prompts were deleted (admin-token-vestige + status-dashboard
#    duplicate) — these tests fail loudly if either ever re-appears.
# 2. The "admin agent" wording was retired in favour of "operator MCP
#    session" (the admin pseudo-agent itself is gone post-Wave-4, see
#    PR #206 / migration 0014). Catalog descriptions and usage strings
#    must not teach the retired concept.
# 3. The wake-loop instructions used to reference tools that don't
#    exist in the registry (`view_messages`, `view_task` singular). The
#    correct names are `get_agent_messages` and `view_tasks`.
# ---------------------------------------------------------------------------


def test_deleted_prompts_stay_deleted() -> None:
    """`worker-init-legacy` and `debug-agent-status` were removed in
    the prompt-book cleanup; they must not be re-introduced.

    - `worker-init-legacy` taught the retired `{{ADMIN_TOKEN}}`
      pseudo-agent flow (PRs #203-#212 retired admin_token; PR #206 /
      migration 0014 retired the admin pseudo-agent row itself).
    - `debug-agent-status` duplicated the live Agents dashboard page
      and wasn't worth the maintenance.
    """
    from agent_mcp.prompts import load_catalog

    ids = {p["id"] for p in load_catalog().get("prompts", [])}
    assert "worker-init-legacy" not in ids, (
        "worker-init-legacy teaches the retired {{ADMIN_TOKEN}} flow; "
        "do not re-add"
    )
    assert "debug-agent-status" not in ids, (
        "debug-agent-status duplicates the Agents dashboard page; "
        "do not re-add"
    )


def test_no_prompt_teaches_admin_agent_concept() -> None:
    """No prompt's `description` or `usage` field may mention the
    retired "admin agent" concept (case-insensitive).

    Post-Wave-4 there is no admin pseudo-agent — humans operate the
    server through their own MCP session (the "operator MCP session"
    in the new wording).
    """
    from agent_mcp.prompts import load_catalog

    leaks: list[str] = []
    for p in load_catalog().get("prompts", []):
        for field in ("description", "usage"):
            value = p.get(field, "") or ""
            if "admin agent" in value.lower():
                leaks.append(f"{p['id']}.{field}: {value!r}")
    assert not leaks, (
        "prompts still reference the retired 'admin agent' concept; "
        "use 'operator MCP session' instead. Offenders:\n  - "
        + "\n  - ".join(leaks)
    )


def test_wake_loop_prompt_uses_real_tool_names() -> None:
    """The `agent-mcp-enter-event-loop` prompt must reference tools
    that actually exist in the registry.

    `view_messages` was never registered; the real tool is
    `get_agent_messages` (agent_mcp/tools/agent_communication_tools.py).
    `view_task` (singular) was never registered either; the real tool
    is `view_tasks` (plural, agent_mcp/tools/task_tools.py).

    Workers that paste the prompt and try to call the non-existent
    tools get an immediate registry error — broken onboarding.
    """
    import re

    from agent_mcp.prompts import get_prompt
    from agent_mcp.app.event_loop_instructions import WAKE_LOOP_INSTRUCTIONS

    entry = get_prompt("agent-mcp-enter-event-loop")
    assert entry is not None, "wake-loop catalog entry vanished"
    template = entry["template"]

    # The two consumers (catalog + python constant) MUST stay in sync;
    # check both copies independently.
    for label, text in (
        ("catalog.json template", template),
        ("WAKE_LOOP_INSTRUCTIONS constant", WAKE_LOOP_INSTRUCTIONS),
    ):
        assert "get_agent_messages" in text, (
            f"{label} missing get_agent_messages"
        )
        assert "view_tasks" in text, f"{label} missing view_tasks"
        assert "view_messages" not in text, (
            f"{label} still references non-existent view_messages tool"
        )
        # `view_task,` (with trailing comma) catches the singular form
        # without false-positive on `view_tasks`.
        assert "view_task," not in text, (
            f"{label} still references non-existent view_task "
            "(singular) tool"
        )
        # Also catch `view_task ` (trailing space) and `view_task)`.
        assert not re.search(r"view_task(?![s_])", text), (
            f"{label} still references non-existent view_task "
            "(singular) tool"
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
