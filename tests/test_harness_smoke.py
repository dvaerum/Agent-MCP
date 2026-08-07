"""Smoke test for the `tests/harness.py` E2E harness.

Candidate E from the 2026-06-01 architecture review: collapse the ~40
lines of per-test ASGI-build + lifespan + handshake + ContextVar setup
that every integration test currently re-implements into a single async
context manager exported from `tests/harness.py`.

These tests pin the harness's public contract:

  * `mcp_session(tmp_path)` async context manager yields an `AdminClient`
    with `.admin_token`, `.client` (httpx TestClient), and the MCP-call
    helpers `.call`, `.list_tools`, `.assert_tool_succeeds`,
    `.assert_unauthorized`.

  * `admin.create_worker("alice")` registers a worker via the same SQL
    path the existing tests use and returns a `WorkerSession` whose
    tool calls run with the worker bearer set on `request_auth_token`
    — so `tools/list` filtering and per-tool auth gates see the worker
    role, not the admin role.

  * `admin.create_admin_agent("admin-bot")` returns a `WorkerSession`
    bound to the admin token (admin can drive tools as if it were a
    distinct caller).

  * `.assert_tool_succeeds(...)` raises pytest.fail (`Failed`) when
    the tool returns isError or text starting with "Unauthorized…".

  * `.assert_unauthorized(...)` raises pytest.fail when the call
    unexpectedly succeeds; passes silently when the wire response
    carries an Unauthorized text block.

If any of these contracts change, this file is the single source of
truth — migrated tests in the same PR depend on them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_mcp_session_yields_working_admin_client(tmp_path: Path) -> None:
    """The harness builds the app, runs lifespan, and yields an admin
    client whose `tools/list` returns the full registered catalogue
    (admin sees everything per PR #55)."""
    from agent_mcp.tools.registry import tool_schemas
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        assert admin.admin_token, "admin_token should be populated"
        tools = await admin.list_tools()
        names = {t.name for t in tools}
        registered = {e["name"] for e in tool_schemas}
        assert names == registered, (
            f"admin should see every registered tool; "
            f"missing={sorted(registered - names)}, "
            f"extra={sorted(names - registered)}"
        )


async def test_create_worker_session_uses_worker_bearer(tmp_path: Path) -> None:
    """A WorkerSession returned by `create_worker` must drive tools
    with the worker token bound on `request_auth_token` so the
    backend's role-based filter (PR #55) treats it as a worker.

    Concrete invariant: a fresh worker should NOT see the admin-only
    `view_status` tool in tools/list.
    """
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        tools = await alice.list_tools()
        names = {t.name for t in tools}
        assert "view_status" not in names, (
            f"worker tools/list should not include admin-only "
            f"view_status; got: {sorted(names)}"
        )
        # And the worker can call a tool from its own session: view_tasks
        # is in the "any" set, so should succeed.
        result = await alice.call("view_tasks", {})
        first_text = result[0].text if result else ""
        assert "Unauthorized" not in first_text, (
            f"worker should be able to call view_tasks; got: {first_text}"
        )


async def test_create_admin_agent_uses_admin_bearer(tmp_path: Path) -> None:
    """`create_admin_agent` returns a session bound to the admin
    bearer — admin-only tools succeed through it."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        bot = await admin.create_admin_agent("admin-bot")
        # view_status is admin-only; should not return Unauthorized.
        result = await bot.call("view_status", {})
        first_text = result[0].text if result else ""
        assert "Unauthorized" not in first_text, (
            f"admin-bot session should pass auth on view_status; "
            f"got: {first_text}"
        )


async def test_assert_unauthorized_fails_on_unexpected_success(
    tmp_path: Path,
) -> None:
    """`.assert_unauthorized()` must raise pytest.fail when the call
    returns success instead of an Unauthorized text block."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        # Admin calling view_tasks succeeds → assert_unauthorized
        # should call pytest.fail (which raises pytest.fail.Exception
        # a.k.a. Failed).
        with pytest.raises(BaseException) as excinfo:
            await admin.assert_unauthorized("view_tasks", {})
        # pytest.fail raises a subclass of BaseException called
        # `_pytest.outcomes.Failed`; check by class name to avoid
        # depending on the private import path.
        assert type(excinfo.value).__name__ in {"Failed", "AssertionError"}, (
            f"expected pytest.fail-shaped exception; "
            f"got {type(excinfo.value).__name__}: {excinfo.value}"
        )


async def test_assert_unauthorized_passes_on_actual_unauth(
    tmp_path: Path,
) -> None:
    """`.assert_unauthorized()` must succeed (not raise) when the
    call truly returns an Unauthorized response — e.g. a worker
    calling view_status."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        # view_status is admin-only; worker call returns Unauthorized
        # text. The assert helper should NOT raise.
        await alice.assert_unauthorized("view_status", {})


async def test_assert_tool_succeeds_fails_on_iserror(tmp_path: Path) -> None:
    """`.assert_tool_succeeds()` must raise pytest.fail when the call
    returns isError=True (e.g. validation failure)."""
    from tests.harness import mcp_session

    async with mcp_session(tmp_path) as admin:
        # send_agent_message with a bad enum value triggers a
        # framework-level validation error (isError=True).
        with pytest.raises(BaseException) as excinfo:
            await admin.assert_tool_succeeds(
                "send_agent_message",
                {
                    "recipient_id": "admin",
                    "message": "ping",
                    "priority": "urgent-X",  # not a valid enum
                },
            )
        assert type(excinfo.value).__name__ in {"Failed", "AssertionError"}, (
            f"expected pytest.fail-shaped exception; "
            f"got {type(excinfo.value).__name__}: {excinfo.value}"
        )
