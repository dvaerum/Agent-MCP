"""Unification guard: every MCP-catalog surface derives a caller's role
through the single ``catalog_role`` function, so they never disagree.

Before arch-r3 #1+5 PR-B, three surfaces re-derived "who is this" from
the Principal differently: ``tools/list`` (registry.list_available_tools)
mapped a viewer ``forwarding_header`` caller to ``"anonymous"``, while
``prompts/list`` (main_app._principal_role) mapped the SAME caller to
``"worker"``, and resources string-matched ``agent_id == "admin"``. A
viewer therefore saw a worker-tier prompt but NOT the worker-tier tools
it could actually call.

These tests pin the invariant: for one Principal, ``catalog_role`` and
every surface agree. The viewer ``forwarding_header`` case is RED against
pre-PR-B code (tools/list hid worker-tier tools from a caller prompts
treated as a worker).
"""
from __future__ import annotations

import pytest

import agent_mcp.tools  # noqa: F401 — register tools
from agent_mcp.core.principal_builder import build_operator_principal, catalog_role
from agent_mcp.tools.registry import (
    list_available_tools,
    request_auth_token,
    request_principal,
)


def _viewer_forwarding_principal():
    """A viewer-tier operator arriving via the signed forwarding header —
    authenticated, read-only, NOT an admin."""
    return build_operator_principal(
        user_id="viewer-op",
        kind="forwarding_header",
        project_role="viewer",
        sysadmin=False,
        project_name="proj",
        source_token=None,
    )


def test_catalog_role_viewer_forwarding_header_is_worker() -> None:
    """The canonical answer: a viewer forwarding-header caller is a
    ``"worker"`` (authenticated non-admin), never ``"anonymous"``."""
    assert catalog_role(_viewer_forwarding_principal()) == "worker"


def test_catalog_role_anonymous_and_operator() -> None:
    assert catalog_role(None) == "anonymous"
    operator = build_operator_principal(
        user_id="op",
        kind="forwarding_header",
        project_role="operator",
        sysadmin=False,
    )
    assert catalog_role(operator) == "admin"


@pytest.mark.asyncio
async def test_prompts_surface_matches_catalog_role_for_viewer() -> None:
    """The prompts surface (``_principal_role``) agrees with
    ``catalog_role`` for a viewer forwarding-header caller."""
    from agent_mcp.app.main_app import _principal_role

    principal = _viewer_forwarding_principal()
    cv = request_principal.set(principal)
    try:
        assert _principal_role() == catalog_role(principal) == "worker"
    finally:
        request_principal.reset(cv)


@pytest.mark.asyncio
async def test_tools_list_surface_matches_catalog_role_for_viewer() -> None:
    """The tools/list surface agrees with ``catalog_role``: a viewer
    forwarding-header caller (role ``"worker"``) sees the worker-tier
    tools it can call — e.g. ``view_tasks`` (cap ``tasks.view``, which
    the viewer bundle grants).

    RED against pre-PR-B code: ``list_available_tools`` mapped the viewer
    to ``"anonymous"`` and hid every worker-tier tool, disagreeing with
    the prompts surface that treated the same caller as a worker.
    """
    principal = _viewer_forwarding_principal()
    assert catalog_role(principal) == "worker"
    tools = await list_available_tools(principal=principal)
    names = {t.name for t in tools}
    assert "view_tasks" in names, (
        "viewer forwarding-header caller (a worker for the catalog) should "
        f"see worker-tier view_tasks; saw {sorted(names)}"
    )


@pytest.mark.asyncio
async def test_resources_surface_uses_catalog_role_for_admin_agent(tmp_path) -> None:
    """The resources cross-agent read gate rejoins the Principal model:
    an ``agent_id == "admin"`` caller (catalog_role ``"admin"``) may read
    another agent's resource; a worker may not. This replaces the bare
    ``bearer_agent_id == "admin"`` string test with ``catalog_role``.
    """
    from tests.harness import mcp_session
    import mcp.types as mcp_types
    from pydantic_core import Url

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        handler = admin._mcp_app_instance().request_handlers[
            mcp_types.ReadResourceRequest
        ]
        # Admin reads alice's status (cross-agent) — allowed.
        req = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(
                uri=Url("agent-mcp://status/alice")
            ),
        )
        tok = request_auth_token.set(admin.admin_token)
        try:
            result = await handler(req)
        finally:
            request_auth_token.reset(tok)
        inner = result.root if hasattr(result, "root") else result
        assert getattr(inner, "contents", None), "admin cross-agent read failed"

        # Worker alice reads bob's status — rejected.
        await admin.create_worker("bob")
        req2 = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(
                uri=Url("agent-mcp://status/bob")
            ),
        )
        tok2 = request_auth_token.set(alice.token)
        try:
            with pytest.raises(Exception):
                await handler(req2)
        finally:
            request_auth_token.reset(tok2)
