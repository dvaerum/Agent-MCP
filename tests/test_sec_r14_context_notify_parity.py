"""BL-R14-1: REST-vs-MCP post-write NOTIFY PARITY on the two
operator-reachable settings write surfaces.

Wave 11 (ADR-0016): the ``config_*`` toggles live in the dedicated
``project_settings`` store now — the REST surface is
``PUT /api/settings/<key>`` and the MCP surface is
``update_project_settings`` (the context tools reject the config
namespace outright). The parity invariant is unchanged; only the
surfaces moved.

Both write surfaces must fire the SAME wake set for the key they
changed:

* ``config_allow_worker_*`` (worker-capability toggle) → push
  ``notifications/tools/list_changed`` (``_emit_tools_list_changed``)
  so subscribed workers re-fetch ``tools/list`` and can see/invoke a
  newly granted tool without waiting for a periodic refresh.
* ``config_auto_event_loop_global`` (global event-loop toggle) →
  ``wake_all_for_flag_recheck`` so in-flight ``wait_for_events``
  re-evaluate and return ``stop_listening`` when the flag flips OFF.

Before the fix each surface fired only ONE of the two:

* REST ``/api/memories`` create/update fired
  ``wake_all_for_flag_recheck`` for the loop toggle but OMITTED
  ``_emit_tools_list_changed`` for the worker-capability toggle — so
  an operator granting a worker a tool from the dashboard never
  pushed ``tools/list_changed``.
* MCP ``update_project_context`` did the REVERSE — emitted
  ``tools/list_changed`` but OMITTED ``wake_all_for_flag_recheck`` —
  so flipping the global loop toggle over MCP didn't wake in-flight
  waiters.

This mirrors the notify-parity class the loop fixed before
(BL-R7-1/R8-1/R13-3). The wakes assert against a spy on the two wake
functions, same pattern as ``test_tools_list_changed_on_policy_toggle``
and ``test_event_coord_mid_flight_stop``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _rest_upsert(admin, key: str, value):
    """Upsert a settings row through the REST write surface —
    ``PUT /api/settings/<key>`` (upsert semantics; dispatches the gated
    ``update_project_settings`` tool underneath)."""
    return admin.request(
        "PUT",
        f"/api/settings/{key}",
        json={"context_value": value},
    )


# ── Direction 1: REST worker-policy write → tools/list_changed ───────


async def test_rest_worker_toggle_pushes_tools_list_changed(
    tmp_path: Path,
) -> None:
    """Granting a worker capability via REST
    ``/api/settings`` (``config_allow_worker_*``) must push
    ``tools/list_changed`` so connected workers refetch ``tools/list``.
    On main the REST surface omitted this call entirely."""
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            r = _rest_upsert(
                admin, "config_allow_worker_self_assign", True
            )
            assert r.status_code == 200, r.text
            assert emit.called, (
                "REST write of config_allow_worker_* must push "
                "tools/list_changed; _emit_tools_list_changed was "
                "never called"
            )


# ── Direction 2: MCP loop-toggle write → wake_all_for_flag_recheck ───


async def test_mcp_loop_toggle_wakes_waiters(tmp_path: Path) -> None:
    """RED before fix: flipping the global event-loop toggle via MCP
    ``update_project_context`` must call ``wake_all_for_flag_recheck``
    so in-flight ``wait_for_events`` re-evaluate. On main the MCP
    surface omitted this call entirely."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {
                    "context_key": "config_auto_event_loop_global",
                    "context_value": False,
                    "description": "flip loop OFF",
                },
            )
            assert wake.called, (
                "MCP update_project_context of "
                "config_auto_event_loop_global must call "
                "wake_all_for_flag_recheck; it was never called"
            )


# ── Parity guards: the pre-existing wake on each side still fires ────


async def test_rest_loop_toggle_still_wakes(tmp_path: Path) -> None:
    """The REST surface's pre-existing loop-toggle wake must survive
    the parity fix (don't regress the one wake that worked)."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            r = _rest_upsert(admin, "config_auto_event_loop_global", False)
            assert r.status_code == 200, r.text
            assert wake.called, (
                "REST write of config_auto_event_loop_global must "
                "still call wake_all_for_flag_recheck"
            )


async def test_mcp_worker_toggle_still_pushes(tmp_path: Path) -> None:
    """The MCP surface's pre-existing worker-policy push must survive
    the parity fix."""
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {
                    "context_key": "config_allow_worker_self_assign",
                    "context_value": True,
                    "description": "grant self-assign",
                },
            )
            assert emit.called, (
                "MCP update_project_context of config_allow_worker_* "
                "must still push tools/list_changed"
            )


# ── Regression: don't over-fire the *other* wake, and don't fire on
#    an unrelated key. ────────────────────────────────────────────────


async def test_mcp_worker_toggle_does_not_wake_waiters(
    tmp_path: Path,
) -> None:
    """A worker-policy toggle is NOT a loop toggle — it must not cost
    an event-loop wakeup."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {
                    "context_key": "config_allow_worker_self_assign",
                    "context_value": True,
                    "description": "grant",
                },
            )
            assert not wake.called, (
                "worker-policy toggle should NOT fire "
                "wake_all_for_flag_recheck; "
                f"got call(s): {wake.call_args_list}"
            )


async def test_rest_loop_toggle_does_not_push_tools_list(
    tmp_path: Path,
) -> None:
    """The loop toggle changes no worker tool visibility — it must not
    push ``tools/list_changed``."""
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit:
            r = _rest_upsert(admin, "config_auto_event_loop_global", False)
            assert r.status_code == 200, r.text
            assert not emit.called, (
                "loop toggle should NOT push tools/list_changed; "
                f"got call(s): {emit.call_args_list}"
            )


async def test_unrelated_rest_write_fires_neither_wake(
    tmp_path: Path,
) -> None:
    """A plain (non-config) memory write must fire NEITHER wake —
    otherwise every project_context write costs two wakeups."""
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            r = admin.post(
                "/api/memories",
                json={"context_key": "team_motto", "context_value": "ship the v1"},
            )
            assert r.status_code == 200, r.text
            assert not emit.called, (
                "unrelated write must not push tools/list_changed; "
                f"got: {emit.call_args_list}"
            )
            assert not wake.called, (
                "unrelated write must not wake waiters; "
                f"got: {wake.call_args_list}"
            )


async def test_unrelated_mcp_write_fires_neither_wake(
    tmp_path: Path,
) -> None:
    """MCP counterpart of the unrelated-write regression guard."""
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "update_project_context",
                {
                    "context_key": "team_motto",
                    "context_value": "ship the v1",
                },
            )
            assert not emit.called, (
                "unrelated MCP write must not push tools/list_changed; "
                f"got: {emit.call_args_list}"
            )
            assert not wake.called, (
                "unrelated MCP write must not wake waiters; "
                f"got: {wake.call_args_list}"
            )
