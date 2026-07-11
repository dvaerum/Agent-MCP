"""
Agent-MCP Terminal User Interface (TUI) Package

This package provides the read-only display/runtime plumbing the server
uses to render its startup banner and live status screen
(``tui/runtime.py``, ``tui/display.py``, ``tui/colors.py``). The
interactive menu-driven control surface (main loop, menu, actions,
input) was never wired up — nothing instantiated it — and was removed
as dead code (arch-r3 #6b).
"""
