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
    # ADR-0018: the canonical worker-permission policy keys are no longer
    # hardcoded in the frontend — the backend registry
    # (agent_mcp.core.settings_schema) is the single source of truth, and
    # the dashboard renders itself data-driven from GET /api/settings-schema.
    # Assert the keys against the registry rather than the frontend copy.
    from agent_mcp.core.settings_schema import KNOWN_SETTING_KEYS

    for key in (
        "config_allow_worker_to_worker",
        "config_allow_worker_self_assign",
        "config_allow_worker_update_own_status",
        "config_allow_worker_create_unassigned",
    ):
        assert key in KNOWN_SETTING_KEYS, (
            f"expected {key} in the settings-schema registry "
            "(KNOWN_SETTING_KEYS)"
        )
    # Must consume the schema (ADR-0018) and be backed by the settings
    # endpoints (ADR-0016: config_* lives in project_settings; writes go
    # through updateSetting / createSetting → PUT/POST /api/settings...).
    assert "getSettingsSchema" in src, (
        "expected the component to read the schema via getSettingsSchema "
        "(settings-schema) — ADR-0018 data-driven rendering"
    )
    assert "updateSetting" in src and "createSetting" in src, (
        "expected the component to write via updateSetting/createSetting"
    )
    assert "getSettingsData" in src, (
        "expected the component to read via getSettingsData (settings-data)"
    )
    assert "updateMemory" not in src and "createMemory" not in src, (
        "the Settings tab must no longer write through the memories API "
        "(ADR-0016)"
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
    permission toggles. Stored as
    project_settings["config_message_retention_days"] (ADR-0016).
    """
    # ADR-0018: the retention key + its type are owned by the backend
    # registry, not hardcoded in the frontend. Assert the knob against
    # the schema (single source of truth).
    from agent_mcp.core.settings_schema import KNOWN_SETTING_KEYS, spec_for

    assert "config_message_retention_days" in KNOWN_SETTING_KEYS, (
        "expected config_message_retention_days in the settings-schema "
        "registry (KNOWN_SETTING_KEYS)"
    )
    spec = spec_for("config_message_retention_days")
    assert spec is not None and spec.type == "int", (
        "config_message_retention_days must be an int-typed setting "
        f"(retention is an integer day count); got {spec}"
    )
    # The int widget still renders a numeric input (not a Switch) in the
    # data-driven frontend.
    src = _read("components/dashboard/settings-dashboard.tsx")
    assert 'type="number"' in src or "type='number'" in src, (
        "expected a numeric <input type=\"number\"> for the int widget "
        "(retention days)"
    )
