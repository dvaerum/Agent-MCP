"""Dashboard test for the Role dropdown on Add Agent / Edit Agent.

Phase 2 Wave 2b (prancy-napping-pie §2e) wired the dashboard's role
dropdown so operators can choose ``worker`` (default) or ``manager``
when creating or editing an agent.

Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the legacy
``CreateAgentModal`` along with the spawn-via-tmux
``create_agent_tool_impl``. The sole agent-creation surface is now
``RegisterAgentModal`` (Wave 7 PR 0), which carries its own role
dropdown. Edit-agent flow keeps its role dropdown. The
``apiClient.createAgent`` typing pin retires with the legacy method;
``editAgent`` still carries ``agent_role?:``.

The repo has no jsdom; behaviour is verified via ``npm run build``
plus Firefox-MCP e2e against the VM. The tests here are source-grep
guards (matches ``test_dashboard_add_agent_copy.py`` / friends).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
AGENTS_TSX = DASHBOARD / "components" / "dashboard" / "agents-dashboard.tsx"
API_TS = DASHBOARD / "lib" / "api" / "agents.ts"


def _read(p: Path) -> str:
    # The Agents page is a page module + a directory of satellites since
    # the <DataTablePage> migration (the Register / Edit dialogs each
    # own a file now); the role-dropdown guards read all of it.
    if p == AGENTS_TSX:
        from tests.dashboard_sources import agents_page_source

        return agents_page_source()
    return p.read_text(encoding="utf-8")


# ---------- RegisterAgentModal: Role dropdown + form state -------------


def test_register_agent_modal_state_has_role_field() -> None:
    """The form state object initialised in ``RegisterAgentModal`` must
    carry a ``role`` field with default ``'worker'`` so the
    submit handler can read it back."""
    src = _read(AGENTS_TSX)
    assert "role: 'worker'" in src or 'role: "worker"' in src, (
        "RegisterAgentModal form state must default role to 'worker'"
    )


def test_register_agent_modal_renders_role_select() -> None:
    """The Add Agent dialog must render a Select with manager + worker
    options bound to the form's role field."""
    src = _read(AGENTS_TSX)
    assert 'value="worker"' in src or "value='worker'" in src, (
        "Role dropdown must offer 'worker' as a SelectItem value"
    )
    assert 'value="manager"' in src or "value='manager'" in src, (
        "Role dropdown must offer 'manager' as a SelectItem value"
    )


def test_register_agent_modal_submit_includes_role() -> None:
    """The submit handler must forward ``role`` to the registerAgent
    payload — otherwise the dropdown is decorative and the backend never
    sees it."""
    src = _read(AGENTS_TSX)
    # ``registerAgent({ name: ..., role: formData.role, ... })`` is the
    # literal shape the modal builds. Pin on the substring.
    assert "role: formData.role" in src, (
        "RegisterAgentModal must pass role: formData.role to "
        "apiClient.registerAgent"
    )


# ---------- EditAgentDialog: Role dropdown + diff logic ----------------


def test_edit_agent_dialog_has_agent_role_state() -> None:
    """The Edit Agent dialog must hold ``agentRole`` state, seeded from
    the current row's ``agent_role`` (defaulting to 'worker'), so the
    operator can change it."""
    src = _read(AGENTS_TSX)
    assert "useState" in src and "agentRole" in src or "setAgentRole" in src, (
        "EditAgentDialog must declare agentRole state for the Role dropdown"
    )


def test_edit_agent_dialog_diffs_agent_role_into_updates() -> None:
    """The Edit Agent save handler must include ``agent_role`` in the
    updates payload when the dropdown's value differs from the agent's
    current role — matches the diff pattern used for capabilities /
    color / working_directory / aoe_session_id / auto_event_loop."""
    src = _read(AGENTS_TSX)
    assert "updates.agent_role" in src, (
        "EditAgentDialog must assign updates.agent_role when the role "
        "field has changed"
    )


# ---------- api.ts client typings carry agent_role --------------------


def test_api_client_edit_agent_typing_includes_agent_role() -> None:
    """``apiClient.editAgent``'s ``updates`` arg type must accept
    ``agent_role`` so the EditAgentDialog can pass it.

    Wave 7 PR 3 deleted ``apiClient.createAgent`` (legacy spawn path);
    the equivalent typing pin for ``createAgent`` retired with it.
    """
    src = _read(API_TS)
    assert "agent_role?:" in src, (
        "apiClient.editAgent updates type must declare optional "
        "agent_role?: (worker | manager)"
    )
