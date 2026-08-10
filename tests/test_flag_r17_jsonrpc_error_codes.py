"""FLAG-R17-1 — JSON-RPC error-code fidelity on the MCP surface.

The MCP SDK dispatcher (``mcp.server.lowlevel.server``) only preserves a
handler-supplied error ``code`` when the handler raises an
:class:`mcp.shared.exceptions.McpError`; any other exception collapses
into its catch-all and is emitted as ``ErrorData(code=0, ...)`` — and
``0`` is not a valid JSON-RPC error code.

The ``prompts/get`` and ``resources/read`` handlers used to raise bare
``ValueError`` / ``PermissionError``, so both surfaced controlled
failures as ``code: 0``. These tests pin the fix: each controlled
failure now surfaces a spec-valid code (``-32602`` invalid params for
unknown name/URI, ``-32603`` for authorization failures) WITHOUT losing
the pre-existing ``ValueError`` / ``PermissionError`` exception contract
that in-process callers depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError
from pydantic_core import Url

import agent_mcp.prompts as prompts_mod
from agent_mcp.tools.registry import request_auth_token
from tests.harness import mcp_session

_FAKE_CATALOG = {
    "categories": [{"id": "t", "name": "T", "description": "", "icon": "X"}],
    "prompts": [
        {
            "id": "public-hello",
            "title": "Public Hello",
            "description": "",
            "category": "t",
            "visibility": "any",
            "template": "hello={{X}}",
            "variables": [{"name": "X", "description": "", "required": False}],
        },
        {
            "id": "admin-secret",
            "title": "Admin Secret",
            "description": "",
            "category": "t",
            "visibility": "admin",
            "template": "secret={{X}}",
            "variables": [{"name": "X", "description": "", "required": False}],
        },
    ],
}


async def _call_get_prompt(admin, session, name):
    handler = admin._mcp_app_instance().request_handlers[
        mcp_types.GetPromptRequest
    ]
    req = mcp_types.GetPromptRequest(
        method="prompts/get",
        params=mcp_types.GetPromptRequestParams(name=name, arguments={"X": "v"}),
    )
    tok = request_auth_token.set(session.token)
    try:
        return await handler(req)
    finally:
        request_auth_token.reset(tok)


async def _call_read_resource(admin, session, uri):
    handler = admin._mcp_app_instance().request_handlers[
        mcp_types.ReadResourceRequest
    ]
    req = mcp_types.ReadResourceRequest(
        method="resources/read",
        params=mcp_types.ReadResourceRequestParams(uri=Url(uri)),
    )
    tok = request_auth_token.set(session.token)
    try:
        return await handler(req)
    finally:
        request_auth_token.reset(tok)


# ── prompts/get ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompts_get_unknown_name_is_invalid_params(tmp_path: Path) -> None:
    """An unknown prompt name → JSON-RPC -32602 (invalid params), NOT
    the catch-all ``code: 0``. The raised exception stays a ValueError
    so in-process ``except ValueError`` callers keep working."""
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(_FAKE_CATALOG))
    prompts_mod._reload_catalog_for_tests(p)
    try:
        async with mcp_session(tmp_path) as admin:
            with pytest.raises(McpError) as exc:
                await _call_get_prompt(admin, admin, "no-such-prompt")
    finally:
        prompts_mod._reload_catalog_for_tests(None)

    assert exc.value.error.code == mcp_types.INVALID_PARAMS == -32602
    assert exc.value.error.code != 0
    assert isinstance(exc.value, ValueError)


@pytest.mark.asyncio
async def test_prompts_get_forbidden_has_valid_code_not_zero(
    tmp_path: Path,
) -> None:
    """A worker requesting an admin-only prompt → a spec-valid JSON-RPC
    code (-32603), NOT ``code: 0``. The raised exception stays a
    PermissionError so the existing exception contract is preserved."""
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(_FAKE_CATALOG))
    prompts_mod._reload_catalog_for_tests(p)
    try:
        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice-r17")
            with pytest.raises(McpError) as exc:
                await _call_get_prompt(admin, alice, "admin-secret")
    finally:
        prompts_mod._reload_catalog_for_tests(None)

    assert exc.value.error.code == mcp_types.INTERNAL_ERROR == -32603
    assert exc.value.error.code != 0
    assert isinstance(exc.value, PermissionError)


# ── resources/read ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resources_read_unknown_uri_is_invalid_params(
    tmp_path: Path,
) -> None:
    """An unknown resource URI → JSON-RPC -32602 (invalid params), NOT
    ``code: 0``. Stays a ValueError for in-process callers."""
    async with mcp_session(tmp_path) as admin:
        with pytest.raises(McpError) as exc:
            await _call_read_resource(admin, admin, "agent-mcp://bogus/x")

    assert exc.value.error.code == mcp_types.INVALID_PARAMS == -32602
    assert exc.value.error.code != 0
    assert isinstance(exc.value, ValueError)


@pytest.mark.asyncio
async def test_resources_read_cross_agent_has_valid_code_not_zero(
    tmp_path: Path,
) -> None:
    """A worker reading ANOTHER agent's status → a spec-valid JSON-RPC
    code (-32603), NOT ``code: 0``. Stays a ValueError so the existing
    ``pytest.raises(ValueError)`` cross-agent guards keep passing."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice-r17")
        await admin.create_worker("bob-r17")
        with pytest.raises(McpError) as exc:
            await _call_read_resource(
                admin, alice, "agent-mcp://status/bob-r17"
            )

    assert exc.value.error.code == mcp_types.INTERNAL_ERROR == -32603
    assert exc.value.error.code != 0
    assert isinstance(exc.value, ValueError)
