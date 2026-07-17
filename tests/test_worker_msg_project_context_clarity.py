"""Worker-facing message clarity for the project_context tools.

Three worker-message defects fixed here (source change confined to
``agent_mcp/tools/project_context_tools.py``):

1. **config_* rejection.** Writing/deleting a ``config_*`` key on the
   knowledge (memory) path is rejected because the namespace lives in the
   operator-managed project_settings store (ADR-0016). The OLD rejection
   returned ``PermissionDenied`` — which the MCP renderer prefixes with
   ``"Unauthorized: "`` (reads as an unfixable auth failure) — and pointed
   the worker at ``update_project_settings``, an operator-only tool, so
   following the hint earned a SECOND denial (the false-bug loop). The
   rejection is now ``Invalid`` with wording that steers the worker to a
   non-config_* key or to escalate, and names NO unreachable tool.

2. **create_project_context key-exists.** The insert-only conflict used to
   say only "Memory with this key already exists" — no hint about the
   insert-only contract or the update tool. It now names the key and
   points at ``update_project_context``.

3. **DB-error info leak (SD-R9-1 class).** The ``Failed`` arms interpolated
   the raw sqlite3 / SQLAlchemy exception into the worker-facing message,
   leaking schema / column / path internals. They now return a generic
   string; the raw exception survives ONLY in ``logger.error(exc_info=True)``.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    Ok,
    render_as_text_content,
)
from tests.harness import make_principal, mcp_session


pytestmark = pytest.mark.asyncio


# --- principal helpers (mirror tests/test_uow_project_context.py) --------


def _operator_principal(user_id: str = "wm-operator") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker_principal(agent_id: str = "wm-worker") -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token=None,
    )


def _wire(result) -> str:
    """Render a ToolResult exactly as an MCP client would see it."""
    blocks = render_as_text_content(result)
    return blocks[0].text if blocks else ""


# === (a) config_* rejection wording ======================================


async def test_config_key_write_rejected_with_clear_wording(tmp_path):
    """A worker writing a config_* memory key gets Invalid (NOT the
    Unauthorized-framed PermissionDenied), with no pointer to the
    operator-only update_project_settings tool."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            update_project_context_tool_impl,
        )

        result = await update_project_context_tool_impl(
            {"context_key": "config_foo", "context_value": "v"},
            principal=_worker_principal("w-cfg"),
        )

        assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"

        wire = _wire(result)
        # New wording present …
        assert "config_*" in wire, wire
        assert "project settings store" in wire, wire
        # … and the two anti-patterns are gone.
        assert "Unauthorized" not in wire, (
            f"config_* rejection must NOT render as Unauthorized: {wire!r}"
        )
        assert "update_project_settings" not in wire, (
            f"must not point a worker at the operator-only tool: {wire!r}"
        )
        assert "ADR-0016" not in wire, f"drop the internal ADR jargon: {wire!r}"


async def test_config_key_write_rejected_for_admin_same_wording(tmp_path):
    """The rejection is unconditional — an operator writing config_* on the
    knowledge path gets the same Invalid wording (no Unauthorized)."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        result = await create_project_context_tool_impl(
            {"context_key": "config_bar", "context_value": "v"},
            principal=_operator_principal("op-cfg"),
        )

        assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"
        wire = _wire(result)
        assert "Unauthorized" not in wire, wire
        assert "update_project_settings" not in wire, wire
        assert "project settings store" in wire, wire


async def test_config_key_delete_rejected_no_unauthorized(tmp_path):
    """Deleting a config_* key on the context path is rejected with the
    same clear wording, not Unauthorized."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            delete_project_context_tool_impl,
        )

        result = await delete_project_context_tool_impl(
            {"context_key": "config_foo"},
            principal=_worker_principal("w-del"),
        )

        assert isinstance(result, Invalid), f"expected Invalid, got {result!r}"
        wire = _wire(result)
        assert "Unauthorized" not in wire, wire
        assert "update_project_settings" not in wire, wire
        assert "project settings store" in wire, wire


# === (b) create_project_context key-exists wording =======================


async def test_create_key_exists_names_key_and_update_tool(tmp_path):
    async with mcp_session(tmp_path):
        from agent_mcp.tools.project_context_tools import (
            create_project_context_tool_impl,
        )

        first = await create_project_context_tool_impl(
            {"context_key": "wm-dupe", "context_value": "v1"},
            principal=_operator_principal("op-dupe"),
        )
        assert isinstance(first, Ok), f"expected Ok, got {first!r}"

        second = await create_project_context_tool_impl(
            {"context_key": "wm-dupe", "context_value": "v2"},
            principal=_operator_principal("op-dupe"),
        )
        assert isinstance(second, Conflict), f"expected Conflict, got {second!r}"

        wire = _wire(second)
        assert "wm-dupe" in wire, f"must name the offending key: {wire!r}"
        assert "already exists" in wire, wire
        assert "insert-only" in wire, wire
        assert "update_project_context" in wire, (
            f"must steer the worker to the update tool: {wire!r}"
        )


# === (c) DB-error info-leak: generic message, raw only in the log ========


async def test_db_error_returns_generic_message_no_sql_leak(
    tmp_path, monkeypatch
):
    """A forced DB error on the update path returns the generic message —
    NO raw exception / column names in the caller-facing text — while the
    raw exception is logged server-side with exc_info=True."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools import project_context_tools as pctx_mod

        leaky = "no such column: project_context.secret_col"

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError(leaky)

        monkeypatch.setattr(pctx_mod, "_single_update_inline", _boom)

        with patch.object(pctx_mod, "logger") as mock_logger:
            result = await pctx_mod.update_project_context_tool_impl(
                {"context_key": "wm-dberr", "context_value": "v"},
                principal=_operator_principal("op-dberr"),
            )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"

        # Caller-facing text is the generic string — nothing leaked.
        assert result.message == pctx_mod._GENERIC_DB_ERROR
        assert "secret_col" not in result.message
        assert "no such column" not in result.message
        # The generic message is also what the wire renders (SEC-R8-1).
        assert "secret_col" not in _wire(result)

        # The raw exception survives ONLY in the server-side log.
        assert mock_logger.error.called
        logged = " ".join(str(a) for c in mock_logger.error.call_args_list
                           for a in c.args)
        assert leaky in logged, "raw exception must reach the logger"
        assert mock_logger.error.call_args.kwargs.get("exc_info") is True


async def test_db_error_on_create_path_is_generic(tmp_path, monkeypatch):
    """Same guarantee on the create path — no schema leak on failure."""
    async with mcp_session(tmp_path):
        from agent_mcp.tools import project_context_tools as pctx_mod

        leaky = "no such table: project_context_shadow"

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError(leaky)

        monkeypatch.setattr(pctx_mod, "_create_context_inline", _boom)

        result = await pctx_mod.create_project_context_tool_impl(
            {"context_key": "wm-createerr", "context_value": "v"},
            principal=_operator_principal("op-createerr"),
        )

        assert isinstance(result, Failed), f"expected Failed, got {result!r}"
        assert result.message == pctx_mod._GENERIC_DB_ERROR
        assert "project_context_shadow" not in result.message
        assert "project_context_shadow" not in _wire(result)
