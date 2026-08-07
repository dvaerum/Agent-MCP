"""Regression — cookie operator gets its capability bundle over the MCP wire.

The per-project backend builds a per-request :class:`Principal` in
:func:`agent_mcp.app.main_app._build_principal_from_request`. For a
cookie-authenticated operator arriving via the router's signed
forwarding header, that builder used to hard-code
``project_role=None`` — so :func:`resolve_capabilities` never unioned
the operator bundle AND ``has_capability``'s project-membership gate
rejected every non-``system.*`` cap. The operator ended up with zero
capabilities over ``/mcp/<project>``, which broke:

  * ``tools/call register_agent`` (and terminate/create) with
    "Unauthorized: ... required for admin tools", and
  * ``tools/list`` visibility — admin tools hidden from the operator.

The identical REST path (``routers/agents.py``) worked because it
hard-codes ``project_role="operator"``. Wave 9 (PR 3 has_capability
migration + PR 6 has_role bridge deletion) moved the admin-tool and
tool-visibility gates onto capability checks but never updated this
forwarding-header builder — nothing tested the forwarding-header cap
path, so the regression slipped. These tests pin the parity.
"""

from __future__ import annotations

import pytest

from agent_mcp.app.main_app import _build_principal_from_request
from agent_mcp.tools import (
    admin_tools as _admin_tools,  # noqa: F401 — registers register_agent
)
from agent_mcp.tools.registry import list_available_tools


def _forwarding_operator_principal(role: str = "operator"):
    """Build the Principal the backend constructs for a cookie operator
    who arrived via the router's signed forwarding header.

    The forwarding branch of the builder never reads ``request``, so a
    ``None`` request is sufficient to exercise it in isolation.

    SEC-1: ``role`` is the real signed per-project role — the builder
    threads it onto ``project_role`` so viewers get viewer caps.
    """
    return _build_principal_from_request(
        request=None,
        bearer_token="",
        forwarding_operator="dennis",
        forwarding_role=role,
    )


def test_forwarding_header_operator_carries_operator_bundle() -> None:
    """A forwarding-header operator (kind="forwarding_header", a
    project member) carries the operator capability bundle — the caps
    that gate the admin tools."""
    principal = _forwarding_operator_principal()

    assert principal.kind == "forwarding_header"
    assert principal.project_role == "operator"
    # The two caps whose absence broke register_agent / terminate over
    # the wire and hid admin tools from tools/list.
    assert principal.has_capability("agents.register") is True
    assert principal.has_capability("system.config.write") is True
    assert principal.has_capability("agents.terminate") is True


@pytest.mark.asyncio
async def test_forwarding_header_operator_sees_register_agent_in_tools_list() -> None:
    """``list_available_tools`` for a forwarding-header operator
    INCLUDES ``register_agent`` — the admin tool is visible, mirroring
    the operator's REST access."""
    principal = _forwarding_operator_principal()

    tools = await list_available_tools(principal=principal)
    names = {t.name for t in tools}

    assert "register_agent" in names


def test_forwarding_header_viewer_does_not_carry_operator_bundle() -> None:
    """SEC-1: a viewer-tier forwarding-header operator carries ONLY the
    viewer (read-only) bundle — NOT the operator write caps.

    Pre-SEC-1 the builder hard-coded ``project_role="operator"`` for
    every verified header, so a viewer got ``agents.register`` /
    ``agents.terminate`` / ``system.config.write`` over the MCP wire
    (a privilege escalation past the REST ``/api/`` surface, which
    correctly 403'd them). This pins the fix: the viewer's real signed
    role rides the header and yields viewer caps."""
    principal = _forwarding_operator_principal(role="viewer")

    assert principal.kind == "forwarding_header"
    assert principal.project_role == "viewer"
    # Read caps present (viewer is authenticated + a project member).
    assert principal.has_capability("agents.view") is True
    assert principal.has_capability("tasks.view") is True
    # Write / admin caps ABSENT — the escalation is closed.
    assert principal.has_capability("agents.register") is False
    assert principal.has_capability("agents.terminate") is False
    assert principal.has_capability("system.config.write") is False
    assert principal.has_capability("tasks.delete") is False
    assert principal.has_capability("memories.delete") is False
    assert principal.has_capability("rag.rebuild") is False


@pytest.mark.asyncio
async def test_forwarding_header_viewer_does_not_see_register_agent() -> None:
    """SEC-1: ``list_available_tools`` for a viewer-tier
    forwarding-header operator EXCLUDES ``register_agent`` — the admin
    tool is hidden, mirroring the viewer's lack of REST write access."""
    principal = _forwarding_operator_principal(role="viewer")

    tools = await list_available_tools(principal=principal)
    names = {t.name for t in tools}

    assert "register_agent" not in names
