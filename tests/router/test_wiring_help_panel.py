"""Regression test for the production NameError in `_wiring_help_panel`.

The deploy-repo router.py contained a stale ``<p>SSE URL: …{escape(sse)}…</p>``
fragment from before the Streamable HTTP migration. ``sse`` was never
defined as a local variable, so every render of the wiring panel raised
``NameError: name 'sse' is not defined`` — visible in production
``journalctl --user -u agent-mcp-router.service`` as a 500 every time
the index page expanded a wiring block.

Phase 1a of the router-upstream plan (prancy-napping-pie) moves the
router source upstream to ``agent_mcp.router.app`` AND removes the
stale SSE URL line in the same surgery. This test pins the fix down at
its new import path, so any future regression that re-introduces an
undefined name in the wiring panel is caught by pytest, not by
production logs.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_wiring_help_panel_renders_without_nameerror(
    router_module, register_project
) -> None:
    """Render the wiring panel for a registered project — must not raise.

    The bug surface was: any HTML render that hit the
    ``<p>SSE URL: <code>{escape(sse)}</code></p>`` f-string crashed
    with ``NameError``. We don't care about the rendered HTML's
    contents here; we care that the function returns a string at all.
    """
    register_project("nameerror-probe")
    # We pass opened=True so the panel renders eagerly (not a stub).
    html = await router_module._wiring_help_panel(
        "nameerror-probe", opened=True, selected_agent="Admin"
    )
    assert isinstance(html, str)
    assert "nameerror-probe" in html
    # The fix removes the entire "SSE URL:" line; the wiring panel
    # should no longer mention SSE at all.
    assert "SSE URL" not in html
