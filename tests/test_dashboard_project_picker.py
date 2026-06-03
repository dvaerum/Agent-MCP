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
    """As of Phase 3.5b the picker reads the project list from the
    cross-project useProjectsStore (backed by /agent-mcp/__overview)
    instead of fetching /agent-mcp/__projects directly. The store
    indirection lets the picker consume the same envelope the
    overview cards do — one network round-trip per tab, and the
    tenancy mode (multi vs single) is available in the same payload.
    """
    src = PICKER.read_text()
    assert "useProjectsStore" in src, (
        "expected picker to consume useProjectsStore (the cross-"
        "project store backed by /agent-mcp/__overview) instead of "
        "fetching /__projects directly"
    )


def test_picker_navigates_via_window_location() -> None:
    src = PICKER.read_text()
    assert "window.location.href" in src, (
        "expected picker to navigate via window.location.href = "
        "appUrl(<name>) rather than switching server-store entries "
        "(PR-B routes through lib/urls.ts helpers)"
    )
    # PR-B centralised URLs in lib/urls.ts; picker now imports
    # `appUrl()` instead of templating the URL inline.
    assert "appUrl" in src, (
        "expected the picker to import appUrl() from lib/urls.ts "
        "(PR-B centralisation)"
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
