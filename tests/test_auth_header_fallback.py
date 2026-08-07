"""Authorization: Bearer header is the sole self-auth path.

Per Q6e in the plan: MCP clients (Claude Code et al.) speak HTTP with
`Authorization: Bearer <token>` headers. A Starlette middleware
captures the Authorization header into a `contextvars.ContextVar`;
`dispatch_tool_call` synthesizes an ``agent_bearer`` Principal from it
for direct-call sites that don't thread one explicitly.

token-retirement plan Phase C: the legacy header→``arguments.token``
back-fill is GONE (the ``token`` param is retired from every schema).
These remaining tests exercise the contextvar→Principal path and the
explicit-Principal seam — neither of which is affected by the
back-fill removal. The tests that pinned the now-deleted
arguments-injection behaviour were removed in Phase C.

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


async def test_dispatch_resolves_bearer_from_contextvar(
    tmp_path,
) -> None:
    """When arguments carries no self-auth token and the
    ``request_auth_token`` contextvar is set, ``dispatch_tool_call``
    synthesizes an ``agent_bearer`` Principal from the contextvar and
    the tool admits the caller. This is the header-Bearer-only path —
    the sole self-auth surface after the ``token`` param retirement.

    Uses ``view_tasks``: an ``"any"``-tier tool that admits any agent
    bearer, which is what the contextvar fallback produces.
    ``view_status`` is operator-tier — a bearer alone no longer
    satisfies its inline check; that's the migration's intent.
    """
    from agent_mcp.tools.registry import dispatch_tool_call, request_auth_token

    async with mcp_session(tmp_path) as admin:
        request_auth_token.set(admin.admin_token)

        # view_tasks admits any agent_bearer. With the bearer
        # resolved via the contextvar, dispatch must succeed.
        try:
            result = await dispatch_tool_call("view_tasks", {})  # no token arg
        except Exception as e:
            raise AssertionError(
                f"dispatch_tool_call should resolve bearer from contextvar; "
                f"got: {e}"
            ) from e

        from agent_mcp.core.tool_result import Ok
        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        text = result.message or ""
        assert "Unauthorized" not in text, (
            f"contextvar bearer not resolved: {text}"
        )


async def test_dispatch_without_contextvar_and_without_token_returns_auth_failure(
    tmp_path,
) -> None:
    """No ``arguments.token`` and no bearer ContextVar → auth fails.

    Wave 6 PR 6: no Principal is supplied and the dispatcher's
    arguments-token fallback finds none, so the migrated tool
    receives ``principal=None`` and surfaces
    :class:`PermissionDenied`.
    """
    from agent_mcp.core.tool_result import PermissionDenied
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        request_auth_token,
    )

    async with mcp_session(tmp_path):
        # Make sure the contextvar is empty for this test.
        request_auth_token.set(None)

        result = await dispatch_tool_call("view_status", {})
        assert isinstance(result, PermissionDenied)


async def test_dispatch_admits_view_status_with_operator_session_contextvar(
    tmp_path,
) -> None:
    """An explicit operator-session :class:`Principal` admits
    ``view_status``.

    Wave 6 PR 6 regression guard: this is the production REST seam's
    code path — ``_dispatch_through_tool`` builds the Principal and
    the migrated tool admits via the typed Principal."""
    from agent_mcp.core.tool_result import Ok
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        request_auth_token,
    )

    async with mcp_session(tmp_path):
        request_auth_token.set(None)
        principal = make_principal(
            kind="operator_session",
            user_id="op",
            agent_id=None,
            sysadmin=False,
            project_name=None,
            project_role="operator",
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )
        result = await dispatch_tool_call(
            "view_status", {}, principal=principal,
        )

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
