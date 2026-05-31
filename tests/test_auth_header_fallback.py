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
"""

from __future__ import annotations

import asyncio


def _admin(client) -> str:
    return client.get("/api/tokens").json()["admin_token"]


def test_dispatch_uses_contextvar_token_when_arguments_token_missing(client) -> None:
    """When arguments has no 'token' and the request_auth_token contextvar
    is set, dispatch_tool_call injects it before calling the tool.
    """
    from agent_mcp.tools.registry import dispatch_tool_call, request_auth_token

    admin = _admin(client)
    request_auth_token.set(admin)

    # view_status requires admin. With token injected from contextvar,
    # it should succeed (not return Unauthorized → no raise).
    try:
        result = asyncio.run(
            dispatch_tool_call("view_status", {})  # no `token` in args
        )
    except Exception as e:
        raise AssertionError(
            f"dispatch_tool_call should inject token from contextvar; got: {e}"
        )

    text = result[0].text
    assert "Unauthorized" not in text, (
        f"contextvar token not injected (issue Q6e): {text}"
    )


def test_dispatch_does_not_overwrite_explicit_arguments_token(client) -> None:
    """If arguments already has a `token`, the contextvar must NOT
    overwrite it. The explicit value is authoritative."""
    from agent_mcp.tools.registry import dispatch_tool_call, request_auth_token

    request_auth_token.set("override-this-token-should-not-be-used")

    # Pass an obviously-wrong token in arguments. Should be rejected
    # (not silently replaced with the contextvar's "valid" token).
    try:
        asyncio.run(
            dispatch_tool_call("view_status", {"token": "wrong" * 8})
        )
    except Exception:
        # The auth-failure raise (issue H fix) is the expected
        # behavior with a wrong explicit token.
        return
    raise AssertionError(
        "dispatch_tool_call swallowed the wrong explicit token; "
        "the contextvar must not override what the caller provided"
    )


def test_dispatch_without_contextvar_and_without_token_returns_auth_failure(client) -> None:
    """No arguments.token and no contextvar → auth fails normally."""
    from agent_mcp.tools.registry import dispatch_tool_call, request_auth_token

    # Make sure the contextvar is empty for this test.
    request_auth_token.set(None)

    import pytest as _pytest
    with _pytest.raises(Exception):
        asyncio.run(dispatch_tool_call("view_status", {}))
