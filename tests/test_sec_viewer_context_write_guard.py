"""Security: viewer-tier operators must not mutate project_context.

SEC1-class finding (companion to #273 / #274), HIGH, confirmed live.

The REST surface 403s every viewer mutation at
``router/auth_middleware.py`` (``method in _MUTATION_METHODS and
role != "operator"``). The MCP-wire cookie path, however, signs a
``role="viewer"`` forwarding header and delegates authorization to each
tool. The three project_context write tools
(``update_project_context`` / ``bulk_update_project_context`` /
``delete_project_context``) gated ONLY on identity
(``_requires_authenticated_caller``) plus a per-key creator-ownership
matrix that treats a viewer exactly like a worker — so a read-only
viewer could create arbitrary new context keys and edit / delete its
own. project_context rows are indexed into the RAG corpus that
operators + worker agents consume (``ask_project_rag`` /
``view_project_context`` / ``get_system_prompt``), so this is a
stored-injection / RAG-poisoning primitive from a read-only principal.

Fix: the operator-path write tools now require the operator-held
memories-write capability (``memories.update`` for upserts,
``memories.delete`` for deletes) that viewers do NOT carry. Agent
bearers (worker / manager) are untouched — the gate scopes to
``operator_session`` / ``forwarding_header`` kinds only — so they keep
authoring context governed by the per-key ownership matrix.

These tests exercise the tool impls directly with hand-built
principals (the same style as ``test_wave9_pr3_inline_checks.py``): the
viewer-denial path short-circuits before any DB access, and the
no-regression paths run inside ``mcp_session`` so ``SessionLocal`` is
bound to a real test DB.
"""
from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Invalid, Ok, PermissionDenied
from agent_mcp.tools.project_context_tools import (
    bulk_update_project_context_tool_impl,
    delete_project_context_tool_impl,
    update_project_context_tool_impl,
)
from tests.harness import make_principal, mcp_session


pytestmark = pytest.mark.asyncio


# ── Principal builders ────────────────────────────────────────────


def _operator(*, project_role: str, kind: str = "operator_session") -> Principal:
    return make_principal(
        kind=kind,  # type: ignore[arg-type]
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name="proj",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker(*, agent_id: str = "wkr") -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="dummy-tok",
    )


def _row(key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_context WHERE context_key = ?", (key,)
        )
        r = cursor.fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


# ── (1) viewer-tier operator DENIED on all three write tools ──────


async def test_viewer_operator_session_denied_update() -> None:
    """A viewer-tier ``operator_session`` cannot upsert project context."""
    viewer = _operator(project_role="viewer")
    result = await update_project_context_tool_impl(
        {"context_key": "pt_probe", "context_value": "owned"},
        principal=viewer,
    )
    assert isinstance(result, PermissionDenied), result


async def test_viewer_forwarding_header_denied_update() -> None:
    """Same denial via the signed forwarding-header viewer path — this
    is the exact MCP-wire shape the live repro used."""
    viewer = _operator(project_role="viewer", kind="forwarding_header")
    result = await update_project_context_tool_impl(
        {"context_key": "pt_probe", "context_value": "owned"},
        principal=viewer,
    )
    assert isinstance(result, PermissionDenied), result


async def test_viewer_denied_bulk_update() -> None:
    viewer = _operator(project_role="viewer")
    result = await bulk_update_project_context_tool_impl(
        {"updates": [{"context_key": "pt_probe", "context_value": "owned"}]},
        principal=viewer,
    )
    assert isinstance(result, PermissionDenied), result


async def test_viewer_denied_delete() -> None:
    viewer = _operator(project_role="viewer")
    result = await delete_project_context_tool_impl(
        {"context_key": "pt_probe"},
        principal=viewer,
    )
    assert isinstance(result, PermissionDenied), result


# ── (2) operator-tier caller still succeeds (no regression) ───────


async def test_operator_can_still_update(tmp_path) -> None:
    async with mcp_session(tmp_path):
        op = _operator(project_role="operator")
        result = await update_project_context_tool_impl(
            {"context_key": "op_key", "context_value": "v"},
            principal=op,
        )
        assert isinstance(result, Ok), result
        assert _row("op_key") is not None


async def test_operator_can_still_bulk_update(tmp_path) -> None:
    async with mcp_session(tmp_path):
        op = _operator(project_role="operator")
        result = await bulk_update_project_context_tool_impl(
            {"updates": [{"context_key": "op_bulk", "context_value": "v"}]},
            principal=op,
        )
        assert isinstance(result, Ok), result
        assert _row("op_bulk") is not None


async def test_operator_can_still_delete(tmp_path) -> None:
    async with mcp_session(tmp_path):
        op = _operator(project_role="operator")
        await update_project_context_tool_impl(
            {"context_key": "op_del", "context_value": "v"},
            principal=op,
        )
        result = await delete_project_context_tool_impl(
            {"context_key": "op_del"},
            principal=op,
        )
        assert isinstance(result, Ok), result
        assert _row("op_del") is None


# ── (3) worker agent bearer still succeeds (no regression) ────────


async def test_worker_agent_can_still_update(tmp_path) -> None:
    async with mcp_session(tmp_path):
        worker = _worker(agent_id="wkr-A")
        result = await update_project_context_tool_impl(
            {"context_key": "wkr_key", "context_value": "v"},
            principal=worker,
        )
        assert isinstance(result, Ok), result
        row = _row("wkr_key")
        assert row is not None
        assert row["created_by"] == "wkr-A"


async def test_worker_agent_can_still_delete_own_key(tmp_path) -> None:
    async with mcp_session(tmp_path):
        worker = _worker(agent_id="wkr-A")
        await update_project_context_tool_impl(
            {"context_key": "wkr_del", "context_value": "v"},
            principal=worker,
        )
        result = await delete_project_context_tool_impl(
            {"context_key": "wkr_del"},
            principal=worker,
        )
        assert isinstance(result, Ok), result
        assert _row("wkr_del") is None


# ── (4) config_* + other-owner guards still hold (defense-in-depth) ─


async def test_worker_still_blocked_on_config_key(tmp_path) -> None:
    """The pre-existing config_* guard must survive the fix.

    Worker-message clarity: the config_* rejection is now ``Invalid``
    (not the Unauthorized-framed ``PermissionDenied``) — the block itself
    is unchanged (no row lands), only the surfaced variant/wording."""
    async with mcp_session(tmp_path):
        worker = _worker(agent_id="wkr-A")
        result = await update_project_context_tool_impl(
            {"context_key": "config_foo", "context_value": "v"},
            principal=worker,
        )
        assert isinstance(result, Invalid), result
        assert _row("config_foo") is None


async def test_worker_still_blocked_on_other_owner_key(tmp_path) -> None:
    """Worker-B cannot overwrite worker-A's key — ownership matrix intact."""
    async with mcp_session(tmp_path):
        worker_a = _worker(agent_id="wkr-A")
        worker_b = _worker(agent_id="wkr-B")
        await update_project_context_tool_impl(
            {"context_key": "shared", "context_value": "A"},
            principal=worker_a,
        )
        result = await update_project_context_tool_impl(
            {"context_key": "shared", "context_value": "hacked"},
            principal=worker_b,
        )
        assert isinstance(result, PermissionDenied), result
        row = _row("shared")
        assert row is not None
        import json

        assert json.loads(row["value"]) == "A"
