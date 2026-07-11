"""arch-r4 #11c: TUI version display must derive from the real version.

``agent_mcp/core/config.py`` used to hand-maintain ``VERSION = "2.0"``,
separate from the ``importlib.metadata`` / ``pyproject.toml``
single-source-of-truth path ``agent_mcp.__version__`` uses (see
``tests/test_version_single_source.py``). The TUI banner/credits
(``agent_mcp/tui/display.py``) rendered that stale literal, so it kept
showing "2.0" long after the real version moved to 5.4.0. This pins
the TUI module onto the real version instead.
"""

from __future__ import annotations

import pytest

import agent_mcp
from agent_mcp.tui import display as display_module


def test_tui_display_version_matches_dunder_version() -> None:
    assert display_module.VERSION == agent_mcp.__version__


def test_tui_display_version_is_not_the_stale_literal() -> None:
    # The historical drift this guards against: config.py's hand-
    # maintained VERSION had frozen at "2.0" while pyproject.toml (and
    # agent_mcp.__version__) moved on to 5.4.0+.
    assert display_module.VERSION != "2.0"


def test_draw_header_renders_the_real_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end through the actual rendering call: the printed
    credits/version line carries the real version, not "2.0"."""
    # Avoid a real network call to the GitHub releases API during the
    # update-check — irrelevant to what this test pins, and would make
    # the test flaky/slow under CI network policy.
    monkeypatch.setattr(display_module, "requests", None)

    tui = display_module.TUIDisplay()
    tui.draw_header(clear_first=False)

    out = capsys.readouterr().out
    assert f"Version {agent_mcp.__version__}" in out
    assert "Version 2.0" not in out
