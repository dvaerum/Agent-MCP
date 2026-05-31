"""Regression guards for the dashboard Restore + Purge UI.

The Agents page table previously only had `[Terminate]` for active
agents and nothing for terminated agents. This PR adds:

- A `restoreAgent` and `purgeAgent` (with `getPurgePreview`) on
  the api.ts client.
- Buttons in agents-dashboard.tsx that fire on terminated rows.
- A confirmation modal rendering blast-radius counts before purge.
- Removal of the dashboard filter that hides terminated agents.

These tests parse the .tsx/.ts files as text (no jsdom/RTL — matching
the convention in test_dashboard_task_actions.py). They catch
regression if someone removes the wiring.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_api_client_has_restore_agent() -> None:
    src = _read("lib/api.ts")
    assert "restoreAgent" in src, (
        "api.ts must export restoreAgent for the dashboard Restore button"
    )
    assert "/restore" in src, (
        "restoreAgent must hit the /restore endpoint"
    )


def test_api_client_has_purge_agent_and_preview() -> None:
    src = _read("lib/api.ts")
    assert "purgeAgent" in src, "api.ts must export purgeAgent"
    assert "getPurgePreview" in src, (
        "api.ts must export getPurgePreview for the confirmation modal"
    )
    assert "cascade" in src, (
        "purgeAgent must include cascade=true on the DELETE request"
    )
    assert "purge-preview" in src, (
        "getPurgePreview must hit /purge-preview"
    )


def test_agents_dashboard_renders_restore_and_purge() -> None:
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "Restore" in src, (
        "agents-dashboard.tsx must surface a 'Restore' button on terminated rows"
    )
    assert "Purge" in src, (
        "agents-dashboard.tsx must surface a 'Purge' button on terminated rows"
    )
    # The buttons must wire to apiClient methods (handler indirection OK).
    assert "restoreAgent" in src or "handleRestore" in src
    assert "purgeAgent" in src or "handlePurge" in src or "PurgeAgentDialog" in src


def test_agents_dashboard_lists_terminated_agents() -> None:
    """The Agents table previously hid terminated agents (used
    `getActiveAgents()` which filters them out). To show Restore/Purge,
    the page must include terminated rows in the rendered list."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # Heuristic: either no longer uses getActiveAgents-only filter, OR
    # has an explicit reference to terminated rows being included.
    # Accept any one of these signals.
    signals = [
        "showTerminated",
        "includeTerminated",
        "data?.agents",  # iterates raw agents list
        "data.agents",
        "agent.status === 'terminated'",
        "all agents",
    ]
    assert any(s in src for s in signals), (
        "agents-dashboard.tsx must somehow include terminated agents "
        f"in the rendered list; looked for any of {signals}"
    )


def test_purge_dialog_component_exists() -> None:
    """A dedicated purge confirmation dialog component (or inline modal)
    must render the preview counts so admins see blast-radius before
    confirming."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # The dialog must reference the preview counts.
    has_dialog = "Confirm purge" in src or "Purge agent" in src
    assert has_dialog, (
        "agents-dashboard.tsx must contain a purge confirmation modal "
        "with copy like 'Confirm purge' or 'Purge agent'"
    )
    # And must reference the preview shape (counts.*).
    assert "counts" in src or "messages_sent" in src, (
        "purge confirmation must surface the preview counts to the admin"
    )
