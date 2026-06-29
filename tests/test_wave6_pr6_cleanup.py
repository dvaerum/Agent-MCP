"""Wave 6 PR 6 cleanup — negative assertions for the deleted bridge.

This module pins that the symbols PR 6 deleted are gone for good.
The contract:

  * :func:`agent_mcp.core.auth.verify_token` — deleted; identity flows
    via the typed :class:`agent_mcp.core.principal.Principal` carried
    on ``request_principal`` / supplied to ``dispatch_tool_call``.
  * The three operator-session ContextVars
    (``operator_session_active``, ``operator_user_id``,
    ``operator_project_name``) — deleted; the per-request Principal
    replaces them.
  * ``app/deps._bearer_is_operator_tier`` — renamed to
    ``_is_operator_tier_bearer`` and reimplemented against
    ``agent_repo.get_agent_by_token`` (no ``verify_token`` import).
  * ``app/main_app._bearer_has_wake_loop_enabled`` — deleted; the
    eligibility resolved at middleware build time into
    :attr:`Principal.can_wake_loop`.
  * ``app/main_app._caller_role`` — deleted; ``_principal_role()``
    reads from the typed Principal.
  * ``core/authorize._check_role`` — deleted; replaced by
    :func:`_check_role_principal` which takes a Principal.
  * ``dispatch_tool_call``'s ``list[TextContent]`` auto-wrap — deleted;
    every tool returns :data:`ToolResult` directly.

If a future change re-introduces any of these symbols, the
corresponding assertion below fires immediately.
"""

from __future__ import annotations

import pytest


def test_verify_token_is_not_importable() -> None:
    """``core.auth.verify_token`` must not be importable.

    The function was the central indirection that read the now-deleted
    ``operator_session_active`` ContextVar; Wave 6 PR 6 retired the
    whole "boolean role gate" abstraction in favour of
    :meth:`Principal.has_role`.
    """
    with pytest.raises(ImportError):
        from agent_mcp.core.auth import verify_token  # noqa: F401


def test_operator_session_contextvars_are_not_importable() -> None:
    """The three operator-session ContextVars are gone.

    They were the load-bearing back-channel for the legacy
    ``verify_token`` indirection; the typed Principal threaded
    through the dispatcher replaces them.
    """
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import operator_session_active  # noqa: F401
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import operator_user_id  # noqa: F401
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import operator_project_name  # noqa: F401


def test_request_auth_token_contextvar_still_present() -> None:
    """``request_auth_token`` stays — it carries the bearer for the
    Q6e ``arguments["token"]`` fallback that the MCP wire path
    relies on. Wave 6 design notes call this out explicitly."""
    from agent_mcp.tools.registry import request_auth_token  # noqa: F401


def test_request_principal_contextvar_present() -> None:
    """``request_principal`` is the new seam between
    :class:`AuthHeaderMiddleware` and the MCP wire handler — the
    handler reads it back and threads it into
    :func:`dispatch_tool_call`."""
    from agent_mcp.tools.registry import request_principal  # noqa: F401


def test_bridge_helpers_are_not_importable() -> None:
    """The dispatcher's bridge helpers are gone."""
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import (  # noqa: F401
            _derive_principal_from_contextvars,
        )
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import (  # noqa: F401
            _tool_accepts_principal,
        )
    with pytest.raises(ImportError):
        from agent_mcp.tools.registry import (  # noqa: F401
            _wrap_legacy_result_as_ok,
        )


def test_check_role_helper_is_not_importable() -> None:
    """``core.authorize._check_role`` is gone; the replacement is
    :func:`_check_role_principal` which takes a Principal."""
    with pytest.raises(ImportError):
        from agent_mcp.core.authorize import _check_role  # noqa: F401
    # The replacement is present.
    from agent_mcp.core.authorize import _check_role_principal  # noqa: F401


def test_bearer_is_operator_tier_helper_is_renamed() -> None:
    """``app.deps._bearer_is_operator_tier`` is renamed to
    :func:`_is_operator_tier_bearer` and consults ``agent_repo``
    directly (no ``verify_token`` import). The old name must be gone
    so a copy-paste from an older revision doesn't silently shadow
    the new impl."""
    with pytest.raises(ImportError):
        from agent_mcp.app.deps import _bearer_is_operator_tier  # noqa: F401
    from agent_mcp.app.deps import _is_operator_tier_bearer  # noqa: F401


def test_bearer_has_wake_loop_enabled_is_not_importable() -> None:
    """``app.main_app._bearer_has_wake_loop_enabled`` is gone — the
    eligibility folds into :attr:`Principal.can_wake_loop` at
    middleware build time."""
    with pytest.raises(ImportError):
        from agent_mcp.app.main_app import _bearer_has_wake_loop_enabled  # noqa: F401


def test_caller_role_helper_is_not_importable() -> None:
    """``app.main_app._caller_role`` is gone; the prompts handlers
    use :func:`_principal_role` instead."""
    with pytest.raises(ImportError):
        from agent_mcp.app.main_app import _caller_role  # noqa: F401
    from agent_mcp.app.main_app import _principal_role  # noqa: F401


def test_dispatch_tool_call_requires_principal_kwarg_signature() -> None:
    """The dispatcher's signature exposes ``principal`` as a
    keyword-only parameter (the production seams always supply it;
    direct-call fallback synthesizes from ``arguments["token"]``).
    """
    import inspect
    from agent_mcp.tools.registry import dispatch_tool_call

    sig = inspect.signature(dispatch_tool_call)
    assert "principal" in sig.parameters, (
        "dispatch_tool_call must accept a ``principal`` kwarg"
    )
    assert sig.parameters["principal"].kind == inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_tool_result_wrap_helper_no_longer_called_for_lists() -> None:
    """The dispatcher refuses ``list[TextContent]`` returns post-PR-6.

    The bridge that wrapped them as ``Ok(message=...)`` is gone; a
    tool that returns the legacy shape surfaces as :class:`Failed`.
    """
    from typing import Any, Dict, List
    import mcp.types as mcp_types
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        register_tool,
        tool_implementations,
        tool_schemas,
    )
    from agent_mcp.core.tool_result import Failed

    async def _legacy_returner(
        arguments: Dict[str, Any],
    ) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="legacy")]

    register_tool(
        name="_pr6_legacy_shape_probe",
        description="probe",
        input_schema={"type": "object", "properties": {}},
        implementation=_legacy_returner,
    )
    try:
        result = await dispatch_tool_call("_pr6_legacy_shape_probe", {})
        assert isinstance(result, Failed), (
            f"legacy list[TextContent] return must surface as Failed, "
            f"got {result!r}"
        )
        assert "unexpected type list" in result.message.lower()
    finally:
        tool_implementations.pop("_pr6_legacy_shape_probe", None)
        for i, entry in enumerate(list(tool_schemas)):
            if entry.get("name") == "_pr6_legacy_shape_probe":
                tool_schemas.pop(i)
                break
