"""Per-tool authorization decorators consolidate the scattered
verify_token(...) gates from agent_mcp/tools/*.py into a single,
auditable surface in agent_mcp/core/authorize.py.

Post Wave 9 PR 6 the public decorator surface is:

* @requires_capability(cap) — admit iff principal.has_capability(cap).
* @requires_policy(*config_keys, default=...) — operator always allowed;
  worker allowed iff ANY listed project_context key is truthy. Both the
  per-key default (when the row is absent) and the per-key
  enable/disable live in agent_mcp.tools.access._TOGGLE_DEFAULTS /
  project_context.

AuthRejected propagates through dispatch_tool_call → the MCP framework
wrapper sets isError=True on the resulting CallToolResult, replacing
the old `_AUTH_FAILURE_RE` text-matching shim.

Architecture review 2026-06-01 candidate A; Wave 9 PR 6 deleted the
legacy ``@requires`` / ``@requires_role`` decorators tested here.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import mcp.types as mcp_types


# --- AuthRejected propagation -----------------------------------------------


@pytest.mark.asyncio
async def test_authrejected_is_exported() -> None:
    """The dispatcher needs to import AuthRejected from core.authorize."""
    from agent_mcp.core.authorize import AuthRejected

    err = AuthRejected("nope")
    assert str(err) == "nope"
    assert err.reason == "nope"


# --- @requires_policy: toggle-gated worker access ---------------------------


@pytest.mark.asyncio
async def test_requires_policy_admin_always_allowed(reset_globals) -> None:
    """Operator-tier callers bypass the policy check entirely.

    Wave 6 PR 6: identity flows through the typed Principal kwarg;
    the operator-tier admit takes the cookie / forwarding-header
    path without consulting the policy toggle.
    """
    from agent_mcp.core.authorize import requires_policy
    from tests.harness import make_principal

    @requires_policy("config_some_toggle", default=False)
    async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="admin in")]

    p = make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )
    # Even with the toggle defaulting false, operator still gets through.
    result = await my_tool({"token": "anything"}, principal=p)
    assert result[0].text == "admin in"


@pytest.mark.asyncio
async def test_requires_policy_worker_rejected_when_toggle_off(
    project_dir, reset_globals, monkeypatch
) -> None:
    """Worker is rejected when all listed policy keys evaluate false."""
    from agent_mcp.core import globals as g

    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    # Build an app to get the DB schema initialised, then exercise the
    # decorator directly.
    from agent_mcp.app.main_app import create_app
    from starlette.testclient import TestClient

    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        g.active_agents["worker-token"] = {"agent_id": "worker_a"}

        from agent_mcp.core.authorize import requires_policy, AuthRejected

        @requires_policy("config_allow_worker_to_worker", default=False)
        async def my_tool(
            arguments: Dict[str, Any],
        ) -> List[mcp_types.TextContent]:  # pragma: no cover
            return [mcp_types.TextContent(type="text", text="ran")]

        with pytest.raises(AuthRejected):
            await my_tool({"token": "worker-token"})


@pytest.mark.asyncio
async def test_requires_policy_worker_allowed_when_any_toggle_on(
    project_dir, reset_globals, monkeypatch
) -> None:
    """Worker is permitted as long as *one* listed policy toggle is truthy."""
    from agent_mcp.core import globals as g

    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    from agent_mcp.app.main_app import create_app
    from starlette.testclient import TestClient

    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        g.active_agents["worker-token"] = {"agent_id": "worker_a"}

        # Default for config_allow_worker_self_assign is true (per
        # _TOGGLE_DEFAULTS); worker should sail through without us
        # explicitly setting the key.
        from agent_mcp.core.authorize import requires_policy

        @requires_policy(
            "config_allow_worker_self_assign", default=True
        )
        async def my_tool(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
            return [mcp_types.TextContent(type="text", text="worker in")]

        result = await my_tool({"token": "worker-token"})
        assert result[0].text == "worker in"


# --- _is_operator_tier: role/capability-aware, not bare-kind ----------------


def _forwarding_principal(project_role, *, sysadmin=False):
    """Build a forwarding-header Principal with the given project role.

    ``make_principal`` resolves caps via ``resolve_capabilities``, so a
    ``"viewer"`` role yields the read-only bundle and ``"operator"``
    yields the write bundle (which carries ``system.config.write``).
    """
    from tests.harness import make_principal

    return make_principal(
        kind="forwarding_header",
        user_id="op-user",
        agent_id=None,
        sysadmin=sysadmin,
        project_name="proj",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def test_is_operator_tier_excludes_viewer_forwarding_header() -> None:
    """SEC (Finding 1): a viewer-tier forwarding-header principal must
    NOT be treated as operator-tier.

    The old helper gated on ``principal.kind`` alone, so ANY
    forwarding-header identity (including a viewer) collapsed to
    operator-tier and walked past ``requires_policy``. The fix makes
    the check role/capability-aware — a viewer lacks the operator
    write marker and must be excluded.
    """
    from agent_mcp.core.authorize import _is_operator_tier

    viewer = _forwarding_principal("viewer")
    assert _is_operator_tier(viewer) is False


def test_is_operator_tier_admits_operator_forwarding_header() -> None:
    """An operator-role forwarding-header principal stays operator-tier."""
    from agent_mcp.core.authorize import _is_operator_tier

    operator = _forwarding_principal("operator")
    assert _is_operator_tier(operator) is True


def test_is_operator_tier_admits_sysadmin() -> None:
    """A sysadmin (wildcard caps) is always operator-tier, regardless
    of project role."""
    from agent_mcp.core.authorize import _is_operator_tier

    sysadmin = _forwarding_principal(None, sysadmin=True)
    assert _is_operator_tier(sysadmin) is True


@pytest.mark.asyncio
async def test_requires_policy_rejects_viewer_forwarding_header(
    reset_globals,
) -> None:
    """A viewer forwarding-header caller must be rejected at the policy
    gate (it is neither operator-tier nor an agent bearer)."""
    from agent_mcp.core.authorize import requires_policy, AuthRejected

    @requires_policy("config_some_toggle", default=False)
    async def my_tool(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:  # pragma: no cover - must not run
        return [mcp_types.TextContent(type="text", text="viewer in")]

    viewer = _forwarding_principal("viewer")
    with pytest.raises(AuthRejected):
        await my_tool({"token": "anything"}, principal=viewer)


# --- Dispatcher integration -------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_translates_authrejected_to_iserror(
    project_dir, reset_globals, monkeypatch
) -> None:
    """Pre-Wave-6: dispatch_tool_call must propagate AuthRejected as
    an exception so the MCP framework's wrapper sets isError=True
    (replacing the old `_AUTH_FAILURE_RE` text-matching shim).

    Post-Wave-6 PR 5: ``view_status`` is migrated to take a
    :class:`Principal` and return :class:`ToolResult`. Auth failure
    now flows as a typed :class:`PermissionDenied` return rather
    than a raised :class:`AuthRejected`; the REST adapter maps it to
    403 and the MCP wire renderer turns it into ``"Unauthorized:
    ..."`` text. The point this test pins — that ``view_status``
    rejects a garbage token — survives unchanged; only the surface
    shape (returned variant vs. raised exception) flipped with the
    typed-result migration.
    """
    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    from agent_mcp.app.main_app import create_app
    from starlette.testclient import TestClient
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools.registry import dispatch_tool_call

    app = create_app(project_dir=str(project_dir))
    with TestClient(app):
        # view_status is operator-tier. Calling with garbage must be rejected.
        result = await dispatch_tool_call(
            "view_status",
            {"token": "deadbeef" * 4},
        )
        assert isinstance(result, PermissionDenied), (
            f"view_status with garbage token must be rejected; got {result!r}"
        )


@pytest.mark.asyncio
async def test_no_auth_failure_regex_left_behind() -> None:
    """The old `_AUTH_FAILURE_RE` shim must be gone after consolidation.

    If this fails after the GREEN commit, someone re-introduced the
    text-matching escape hatch — defeat the point of the decorator
    refactor.
    """
    from agent_mcp.tools import registry as r

    assert not hasattr(r, "_AUTH_FAILURE_RE"), (
        "registry._AUTH_FAILURE_RE should have been deleted alongside the "
        "decorator migration."
    )


@pytest.mark.asyncio
async def test_get_config_bool_lives_in_one_place() -> None:
    """`_get_config_bool` should exist in exactly one module after
    consolidation — the per-module duplicates in agent_communication_tools
    and task_tools should be gone (callers import from the canonical
    home in core.config or tools.access)."""
    from agent_mcp.tools import agent_communication_tools as ac
    from agent_mcp.tools import task_tools as tt

    assert not hasattr(ac, "_get_config_bool"), (
        "agent_communication_tools._get_config_bool should have been "
        "removed; callers should use the canonical helper."
    )
    assert not hasattr(tt, "_get_config_bool"), (
        "task_tools._get_config_bool should have been removed; callers "
        "should use the canonical helper."
    )
