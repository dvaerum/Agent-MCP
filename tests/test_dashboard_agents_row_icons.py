"""Regression guards for the Agents page row icon refresh.

The Agents table previously surfaced a 3-dot (`MoreVertical`) kebab menu
on every row, but the kebab had no click handlers — it was dead UI.

This PR replaces the kebab with explicit per-row icon buttons and
swaps the existing eye-icon's sidebar drawer for a Dialog-based
detail modal:

- Active rows: Edit (Pencil), Delete (Trash2), View (Eye)
- Terminated rows: Restore + Purge (kept exactly as PR #38 shipped)
- View opens a Dialog modal (not a Sheet / sidebar)
- Edit opens a Dialog modal wired to a new editAgent API client method

Text-parse regression guards (same convention as
test_dashboard_agent_restore_purge.py).
"""

from __future__ import annotations

from tests.dashboard_sources import agents_page_source, read_dashboard


def _read(rel: str) -> str:
    # The Agents page is a page module + a directory of satellites since
    # the <DataTablePage> migration; guards about "the Agents page" read
    # all of it (tests/dashboard_sources.py).
    if rel == "components/dashboard/agents-dashboard.tsx":
        return agents_page_source()
    return read_dashboard(rel)


# ---------- Kebab removed -----------------------------------------


def test_agents_dashboard_no_kebab_menu() -> None:
    """The 3-dot kebab (MoreVertical) had no handlers and was confusing.
    It must be gone from the agents-dashboard.tsx row markup."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "MoreVertical" not in src, (
        "agents-dashboard.tsx must not import or render MoreVertical "
        "(the 3-dot kebab) — every row action should be an explicit icon"
    )
    # Belt-and-suspenders: also the lucide alias.
    assert "MoreHorizontal" not in src, (
        "agents-dashboard.tsx must not render MoreHorizontal either"
    )


# ---------- Edit icon ---------------------------------------------


def test_agents_dashboard_has_edit_icon_button() -> None:
    """Edit button uses the lucide Pencil (or Edit) icon and wires a
    click handler that opens the edit modal."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # Either Pencil or Edit icon must be imported.
    assert ("Pencil" in src) or ("\nimport" in src and "Edit" in src and "Edit2" not in src), (
        "agents-dashboard.tsx must import a Pencil/Edit icon for the "
        "edit-agent row button"
    )
    # The handler indirection — opening the edit dialog from a row click.
    assert ("onEdit" in src) or ("handleEdit" in src) or ("setEditAgent" in src), (
        "agents-dashboard.tsx must wire an edit click handler "
        "(onEdit / handleEdit / setEditAgent) on the row"
    )


def test_agents_dashboard_renders_edit_agent_dialog() -> None:
    """An EditAgentDialog (Dialog-based) must render — title contains
    'Edit agent' so the admin sees what they're doing."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "EditAgentDialog" in src or "Edit agent" in src or "Edit Agent" in src, (
        "agents-dashboard.tsx must include an Edit Agent dialog "
        "(component name EditAgentDialog or title text 'Edit agent')"
    )


# ---------- Delete icon -------------------------------------------


def test_agents_dashboard_has_delete_icon_with_confirmation() -> None:
    """Delete (Trash2) icon on active rows triggers terminate via a
    confirmation dialog — not a bare onClick that immediately POSTs."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # Trash2 is already used by Purge; it must also be used to label
    # the active-row Delete button OR a dedicated TerminateAgentDialog
    # must wrap the confirmation. We accept either signal.
    assert "Trash2" in src, (
        "Trash2 icon must be present (used by Purge already, and reused "
        "for the active-row Delete icon)"
    )
    # The confirmation dialog title text — admin sees this before the
    # destructive action fires.
    assert "Terminate agent" in src, (
        "agents-dashboard.tsx must include a Terminate confirmation "
        "dialog with title text 'Terminate agent ...?'"
    )


# ---------- View / details modal ----------------------------------


def test_view_uses_dialog_not_sheet() -> None:
    """The eye (view) icon used to open `AgentDetailsPanel` — a fixed
    sidebar drawer. It must now open a Dialog modal instead."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "AgentDetailsPanel" not in src, (
        "agents-dashboard.tsx must no longer mount AgentDetailsPanel "
        "(the sidebar drawer); the view-icon now opens a Dialog modal"
    )
    # The new modal component / inline Dialog markup.
    assert "AgentDetailDialog" in src or "Agent details" in src or "Agent Details" in src, (
        "agents-dashboard.tsx must include a dialog-based view modal "
        "(component name AgentDetailDialog or title 'Agent details')"
    )


def test_view_dialog_renders_agent_fields() -> None:
    """The view modal must surface all the agent's fields."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # User-facing labels expected in the dialog body.
    # Working Directory, Color, Token preview, Created.
    for label in (
        "Agent ID",
        "Status",
        "Created",
        "Working Directory",
        "Color",
        "Token",
    ):
        assert label in src, (
            f"view dialog must include the label {label!r}"
        )


# ---------- API client edit endpoint ------------------------------


def test_api_client_has_edit_agent() -> None:
    """A new `editAgent` method must exist on the API client, hitting
    the upstream POST /api/agents/<id>/edit route added by this PR."""
    src = _read("lib/api.ts")
    assert "editAgent" in src, (
        "api.ts must export editAgent for the dashboard Edit button"
    )
    assert "/edit" in src, (
        "editAgent must POST to the /edit endpoint"
    )
