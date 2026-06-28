"""Authorization: Bearer header is a fallback for arguments.token.

Per Q6e in the plan: MCP clients (Claude Code et al.) speak HTTP with
`Authorization: Bearer <token>` headers; agent-mcp's tool surface
expects the token in JSON-RPC `arguments.token`. Today the router
glues the two together via byte-stream rewriting. Moving the glue
upstream lets the router drop that workaround.

Implementation: a Starlette middleware captures the Authorization
header into a `contextvars.ContextVar`. `dispatch_tool_call` checks
the ContextVar and injects into `arguments.token` if missing. Tests
exercise the contextvar path directly (the HTTP middleware path is
trivially correct once the contextvar plumbing works).

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


async def test_dispatch_uses_contextvar_token_when_arguments_token_missing(
    tmp_path,
) -> None:
    """When arguments has no 'token' and the request_auth_token contextvar
    is set, dispatch_tool_call resolves the bearer (Q6e). After Wave 6
    PR 0/5 the resolution happens via the principal bridge:
    ``_derive_principal_from_contextvars`` reads ``request_auth_token``
    and produces an ``agent_bearer`` principal that downstream tools
    consume directly — the dispatcher's legacy ``arguments["token"]``
    injection still happens too, for unmigrated tools that read the
    token field by hand.

    Uses ``view_tasks`` instead of ``view_status``: ``view_tasks`` is
    an ``"any"``-tier tool that admits any agent bearer, which is what
    the contextvar fallback produces. ``view_status`` is operator-tier
    post-Wave-6-PR-5 — a bearer alone no longer satisfies its
    inline check; that's the migration's intent, not a regression in
    the Q6e injection.
    """
    from agent_mcp.tools.registry import dispatch_tool_call, request_auth_token

    async with mcp_session(tmp_path) as admin:
        request_auth_token.set(admin.admin_token)

        # view_tasks admits any agent_bearer. With the bearer
        # resolved via the contextvar, dispatch must succeed.
        try:
            result = await dispatch_tool_call("view_tasks", {})  # no `token`
        except Exception as e:
            raise AssertionError(
                f"dispatch_tool_call should resolve bearer from contextvar; "
                f"got: {e}"
            )

        from agent_mcp.core.tool_result import Ok
        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        text = result.message or ""
        assert "Unauthorized" not in text, (
            f"contextvar token not injected (issue Q6e): {text}"
        )


async def test_dispatch_does_not_overwrite_explicit_arguments_token(
    tmp_path,
) -> None:
    """If arguments already has a `token`, the contextvar must NOT
    overwrite it. The explicit value is authoritative.

    retire-system-token Wave 1: the harness stamps
    ``operator_session_active=True`` by default so admin-tier tools
    admit without a token. Clear it here so the test exercises the
    token-only path the test name describes.

    Wave 6 PR 5: ``view_status`` is migrated to ToolResult — an
    auth-rejection now returns :class:`PermissionDenied` instead of
    raising. The point of this test is to pin that the contextvar
    does NOT silently replace an explicit ``arguments["token"]``;
    asserting on a denial-shaped return is equivalent to the
    pre-migration assertion on an auth-failure raise.
    """
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        request_auth_token,
        operator_session_active,
    )
    from agent_mcp.core.tool_result import Ok, PermissionDenied

    async with mcp_session(tmp_path):
        cv = operator_session_active.set(False)
        try:
            request_auth_token.set("override-this-token-should-not-be-used")

            # Pass an obviously-wrong token in arguments. Should be rejected
            # (not silently replaced with the contextvar's "valid" token).
            result = await dispatch_tool_call(
                "view_status", {"token": "wrong" * 8}
            )
            assert isinstance(result, PermissionDenied), (
                "dispatch_tool_call swallowed the wrong explicit token; "
                "the contextvar must not override what the caller provided"
            )
            assert not isinstance(result, Ok), (
                "view_status should not succeed with a wrong explicit token"
            )
        finally:
            operator_session_active.reset(cv)


async def test_dispatch_without_contextvar_and_without_token_returns_auth_failure(
    tmp_path,
) -> None:
    """No arguments.token and no contextvar → auth fails normally.

    retire-system-token Wave 1: clear the harness's stamped
    ``operator_session_active`` so the auth gate actually fires.

    Wave 6 PR 5: post-migration, the gate's failure surfaces as a
    returned :class:`PermissionDenied` rather than a raised
    ``AuthRejected``.
    """
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        request_auth_token,
        operator_session_active,
    )
    from agent_mcp.core.tool_result import PermissionDenied

    async with mcp_session(tmp_path):
        cv = operator_session_active.set(False)
        try:
            # Make sure the contextvar is empty for this test.
            request_auth_token.set(None)

            result = await dispatch_tool_call("view_status", {})
            assert isinstance(result, PermissionDenied)
        finally:
            operator_session_active.reset(cv)


async def test_dispatch_admits_view_status_with_operator_session_contextvar(
    tmp_path,
) -> None:
    """When ``operator_session_active=True`` is set AND no bearer is
    present, the bridge derives an ``operator_session`` Principal
    that satisfies the operator-tier inline check on ``view_status``.

    Wave 6 PR 5 regression guard: this is the production REST seam's
    code path — ``_dispatch_through_tool`` stamps op_session and the
    migrated tool admits via the typed Principal."""
    from agent_mcp.tools.registry import (
        dispatch_tool_call,
        request_auth_token,
        operator_session_active,
        operator_user_id,
    )
    from agent_mcp.core.tool_result import Ok

    async with mcp_session(tmp_path):
        request_auth_token.set(None)
        cv_op = operator_session_active.set(True)
        cv_user = operator_user_id.set("op")
        try:
            result = await dispatch_tool_call("view_status", {})
        finally:
            operator_user_id.reset(cv_user)
            operator_session_active.reset(cv_op)

        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
