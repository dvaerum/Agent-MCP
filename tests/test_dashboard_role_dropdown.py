"""Dashboard test for the Role dropdown on Add Agent / Edit Agent.

Phase 2 Wave 2b (prancy-napping-pie §2e). The dashboard's
``CreateAgentModal`` and ``EditAgentDialog`` (both in
``agent_mcp/dashboard/components/dashboard/agents-dashboard.tsx``) gain
a Role dropdown bound to ``agent_role`` with options worker / manager
and default ``'worker'``.

The repo has no jsdom; behaviour is verified via ``npm run build``
plus Firefox-MCP e2e against the VM. The tests here are source-grep
guards (matches ``test_dashboard_add_agent_copy.py`` / friends) — they
pin that the Select primitive is wired in with the right values and
that the createAgent / editAgent payload typings carry the field.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
AGENTS_TSX = DASHBOARD / "components" / "dashboard" / "agents-dashboard.tsx"
API_TS = DASHBOARD / "lib" / "api.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------- CreateAgentModal: Role dropdown + form state ---------------


def test_create_agent_modal_state_has_agent_role_field() -> None:
    """The form state object initialised in ``CreateAgentModal`` must
    carry an ``agent_role`` field with default ``'worker'`` so the
    submit handler can read it back."""
    src = _read(AGENTS_TSX)
    # The initial useState for formData must include agent_role: 'worker'.
    assert "agent_role: 'worker'" in src or 'agent_role: "worker"' in src, (
        "CreateAgentModal form state must default agent_role to 'worker'"
    )


def test_create_agent_modal_renders_role_select() -> None:
    """The Add Agent dialog must render a Select with manager + worker
    options bound to the form's agent_role field."""
    src = _read(AGENTS_TSX)
    # Both option values must appear; the literal SelectItem tags pin
    # both — worker and manager.
    assert 'value="worker"' in src or "value='worker'" in src, (
        "Role dropdown must offer 'worker' as a SelectItem value"
    )
    assert 'value="manager"' in src or "value='manager'" in src, (
        "Role dropdown must offer 'manager' as a SelectItem value"
    )


def test_create_agent_modal_submit_includes_agent_role() -> None:
    """The submit handler must forward ``agent_role`` to the onCreateAgent
    payload — otherwise the dropdown is decorative and the backend never
    sees it."""
    src = _read(AGENTS_TSX)
    # `agent_role: formData.agent_role` is the literal we expect inside
    # the onCreateAgent({...}) call. Allow whitespace tolerance.
    assert "agent_role: formData.agent_role" in src, (
        "CreateAgentModal must pass agent_role: formData.agent_role to "
        "onCreateAgent"
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
    # The updates object literal must declare agent_role?: string so
    # the editAgent call's typing accepts it.
    assert "updates.agent_role" in src, (
        "EditAgentDialog must assign updates.agent_role when the role "
        "field has changed"
    )


# ---------- api.ts client typings carry agent_role --------------------


def test_api_client_create_agent_typing_includes_agent_role() -> None:
    """``apiClient.createAgent`` argument type must declare optional
    ``agent_role``. Pre-PR the type was {agent_id, capabilities?,
    working_directory?} — extending it is mandatory so the
    CreateAgentModal call typechecks."""
    src = _read(API_TS)
    # The createAgent signature spans multiple lines; pin the literal.
    assert "agent_role?:" in src, (
        "apiClient.createAgent argument type must declare optional "
        "agent_role?: string (or 'worker' | 'manager')"
    )


def test_api_client_edit_agent_typing_includes_agent_role() -> None:
    """Likewise for ``apiClient.editAgent``: the ``updates`` arg type
    must accept ``agent_role`` so the EditAgentDialog can pass it."""
    src = _read(API_TS)
    # Two occurrences expected (createAgent + editAgent). Pin both via
    # a count >= 2 to surface accidental single-call typing fix.
    assert src.count("agent_role?:") >= 2, (
        "apiClient.editAgent updates type must declare optional "
        "agent_role?: (in addition to createAgent's same field)"
    )
