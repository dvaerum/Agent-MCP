"""Regression guards for the dashboard project picker.

The picker rewrite swaps "switch server-store entries" for "fetch
project list from a router endpoint and navigate via
window.location.href" — the deployment is multi-tenant by URL path,
not by per-server connections.
"""

from __future__ import annotations

from pathlib import Path

PICKER = Path("agent_mcp/dashboard/components/server/project-picker.tsx")


def test_picker_fetches_router_projects_endpoint() -> None:
    src = PICKER.read_text()
    assert "/agent-mcp/__projects" in src, (
        "expected picker to fetch project list from "
        "/agent-mcp/__projects (router endpoint)"
    )


def test_picker_navigates_via_window_location() -> None:
    src = PICKER.read_text()
    assert "window.location.href" in src, (
        "expected picker to navigate via window.location.href = "
        "'/agent-mcp/__dashboard/<name>/' rather than switching "
        "server-store entries"
    )
    assert "/agent-mcp/__dashboard" in src, (
        "expected the dashboard URL pattern in navigation"
    )


def test_picker_drops_add_server_dialog() -> None:
    """Multi-tenant deployment has no concept of manually adding a
    server connection — projects come from the router."""
    src = PICKER.read_text()
    # The "Add Server" dialog used Dialog from @/components/ui/dialog
    assert "Dialog" not in src or "DropdownMenu" in src, (
        "expected the Add-Server Dialog to be removed (or at least "
        "DropdownMenu used as primary UI)"
    )
