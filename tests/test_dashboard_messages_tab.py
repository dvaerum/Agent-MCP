"""Regression guards for the dashboard Messages tab (Phase 6 PR #21).

The tab adds a new top-level dashboard view that lets admins:
- See message history with filters.
- Compose new messages to agents.
- Mark messages read/unread.

Tests verify the component file exists, the view is wired into the
top-level switch, and the navigation sidebar lists it. Regression
guards (parse .ts(x) text) since we don't have jsdom infrastructure;
behavior verified by `npm run build` + manual click-through.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_messages_dashboard_component_exists() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Must export the component used by page.tsx.
    assert "export function MessagesDashboard" in src or "export const MessagesDashboard" in src
    # Must reference the REST endpoint (issue P, PR #20).
    assert "/api/messages" in src, (
        "expected the component to call /api/messages (the new REST endpoint)"
    )


def test_page_routes_messages_view_to_component() -> None:
    src = _read("app/page.tsx")
    assert "MessagesDashboard" in src, (
        "expected page.tsx to import + render MessagesDashboard"
    )
    assert "'messages'" in src or '"messages"' in src, (
        "expected page.tsx switch to include a 'messages' case"
    )


def test_store_view_type_includes_messages() -> None:
    src = _read("lib/store.ts")
    # Find the currentView union and check 'messages' is in it.
    assert "'messages'" in src or '"messages"' in src, (
        "expected store.ts currentView union to include 'messages'"
    )


def test_navigation_lists_messages_tab() -> None:
    src = _read("components/layout/navigation.tsx")
    assert "'messages'" in src or '"messages"' in src, (
        "expected navigation.tsx NavItem entry for view: 'messages'"
    )
    # Common icon for messages is MessageSquare or Mail.
    assert any(
        icon in src for icon in ("MessageSquare", "Mail", "MessagesSquare")
    ), "expected a message-icon import (MessageSquare/Mail/MessagesSquare)"
