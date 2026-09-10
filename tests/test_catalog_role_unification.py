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

The prompts/resources-surface cases were pinned here too until Phase
F's ``agent_mcp/prompts/``/``agent_mcp/resources/`` deletion retired
them; the tools/list case (the one PRE-PR-B regression this file's
own docstring names) stays.
"""
from __future__ import annotations

import pytest

import agent_mcp.tools  # noqa: F401 — register tools
from agent_mcp.core.principal_builder import build_operator_principal, catalog_role
from agent_mcp.tools.registry import list_available_tools


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
