"""Regression guards for the dashboard Settings tab.

The tab adds a new top-level dashboard view that lets admins toggle
per-project worker-permission policies. The toggles are backed by the
existing memory CRUD endpoints — each policy lives in
project_context under a `config_*` key.

Tests verify the component file exists, references the canonical
policy keys, and is wired into the view switch + sidebar nav.
Regression guards (parse .ts(x) text) since we don't have jsdom
infrastructure; behavior verified by `npm run build` + manual
click-through.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_settings_dashboard_component_exists() -> None:
    src = _read("components/dashboard/settings-dashboard.tsx")
    # Must export the component used by page.tsx.
    assert (
        "export function SettingsDashboard" in src
        or "export const SettingsDashboard" in src
    )
    # Must reference all three canonical worker-permission policy keys.
    assert "config_allow_worker_to_worker" in src, (
        "expected the component to expose the config_allow_worker_to_worker toggle"
    )
    assert "config_allow_worker_self_assign" in src, (
        "expected the component to expose the config_allow_worker_self_assign toggle"
    )
    assert "config_allow_worker_update_own_status" in src, (
        "expected the component to expose the config_allow_worker_update_own_status toggle"
    )
    assert "config_allow_worker_create_unassigned" in src, (
        "expected the component to expose the config_allow_worker_create_unassigned "
        "toggle (Q6d — workers filing into the unassigned pool)"
    )
    # Must reference the memory CRUD endpoint it's backed by.
    assert "/memories" in src, (
        "expected the component to PUT/POST to the /memories endpoint"
    )


def test_page_routes_settings_view_to_component() -> None:
    src = _read("app/page.tsx")
    assert "SettingsDashboard" in src, (
        "expected page.tsx to import + render SettingsDashboard"
    )
    assert "'settings'" in src or '"settings"' in src, (
        "expected page.tsx switch to include a 'settings' case"
    )


def test_store_view_type_includes_settings() -> None:
    src = _read("lib/store.ts")
    assert "'settings'" in src or '"settings"' in src, (
        "expected store.ts currentView union to include 'settings'"
    )


def test_navigation_lists_settings_tab() -> None:
    src = _read("components/layout/navigation.tsx")
    assert "'settings'" in src or '"settings"' in src, (
        "expected navigation.tsx NavItem entry for view: 'settings'"
    )
    # Settings tab uses the lucide-react Settings icon.
    assert "Settings" in src, (
        "expected a Settings icon import / NavItem title in navigation.tsx"
    )


def test_settings_dashboard_exposes_message_retention_input() -> None:
    """Phase 6 follow-up (issue Q): admins can configure how many days
    of read agent_messages to keep before the background pruner deletes
    them. The Settings tab must surface this knob alongside the
    permission toggles. Stored as project_context["config_message_retention_days"].
    """
    src = _read("components/dashboard/settings-dashboard.tsx")
    assert "config_message_retention_days" in src, (
        "expected the component to expose the config_message_retention_days "
        "input"
    )
    # Must be a numeric input (not a Switch) — retention is an integer count.
    assert 'type="number"' in src or "type='number'" in src, (
        "expected a numeric <input type=\"number\"> for retention days"
    )
