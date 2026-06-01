"""Tests for `notifications/tools/list_changed` emission on worker
-policy toggle writes (plan Phase 4).

Per `/home/dennis/.knl_unused_intentionally`... no — per
`/home/dennis/.claude/plans/prancy-napping-pie.md` Phase 4:

* Admin writes a `config_allow_worker_*` key to project_context →
  worker tool visibility changes (PR #55 filter reads the toggle
  live) → server emits `notifications/tools/list_changed` so any
  subscribed client immediately re-fetches `tools/list`.

Note on transport reality (stateless StreamableHTTP, per PR #61):

The notification can only be pushed onto the session of the request
that's currently in flight. For an admin calling `update_project_context`
via MCP, that's the admin's response stream — not the worker's open
GET /mcp. Cross-request fan-out to all subscribed workers requires
a custom session registry (same issue as Phase 3's deferred
notification emission). Phase 4 ships the trigger + the helper +
the wiring; cross-session fan-out lands when the registry does.

The tests below pin the trigger fires (helper invoked, notification
attempted) on toggle writes — even if no live session is bound to
push into, the hook MUST execute.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.asyncio


async def test_toggle_write_invokes_emitter(tmp_path: Path) -> None:
    """Writing `config_allow_worker_self_assign` (a worker-policy
    toggle) invokes the `_emit_tools_list_changed` helper after the
    project_context row is committed.

    This is the foundation for the spec-standard notification — the
    push side requires session enumeration not yet built, but the
    trigger must fire so the deferred push lands without source
    changes.
    """
    from tests.harness import mcp_session
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            await admin.assert_tool_succeeds(
                "update_project_context",
                {
                    "context_key": "config_allow_worker_self_assign",
                    "context_value": True,
                    "description": "test toggle",
                },
            )
            assert emit.called, (
                "config_allow_worker_* write must invoke "
                "_emit_tools_list_changed; emit was never called"
            )
            args, kwargs = emit.call_args
            # First positional is the context_key.
            if args:
                assert args[0] == "config_allow_worker_self_assign", (
                    f"helper got wrong key: {args}"
                )


async def test_non_toggle_write_does_not_invoke_emitter(
    tmp_path: Path,
) -> None:
    """Writing a non-toggle key (e.g. a plain notes value) MUST NOT
    fire the notification — otherwise every project_context write
    would cost a wakeup."""
    from tests.harness import mcp_session
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            await admin.assert_tool_succeeds(
                "update_project_context",
                {
                    "context_key": "team_motto",
                    "context_value": "ship the v1",
                },
            )
            assert not emit.called, (
                "non-toggle write should NOT fire "
                "tools/list_changed; got call(s): "
                f"{emit.call_args_list}"
            )


async def test_emitter_is_resilient_to_missing_session(
    tmp_path: Path,
) -> None:
    """`_emit_tools_list_changed` MUST NOT raise when there's no
    MCP session in the request_ctx (e.g. called from a REST endpoint
    or a unit test). It silently no-ops — the toggle write itself
    remains the source of truth and clients re-fetch on the next
    `tools/list` call regardless."""
    from agent_mcp.tools.project_context_tools import (
        _emit_tools_list_changed,
    )

    # Direct call with no request_ctx → must complete without
    # exception. Awaitable per the standard helper signature.
    res = _emit_tools_list_changed("config_allow_worker_self_assign")
    if hasattr(res, "__await__"):
        await res
