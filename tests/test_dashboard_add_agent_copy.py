"""Pin the user-facing copy for the agent-creation flow.

Originally surfaced by Dennis's critical review on 2026-06-17 (against
v5.0.48): the Agents tab had a "Deploy" button + "Deploy Agent" modal
title that did NOT actually deploy anything — the underlying tool only
registered a row + token. The fix renamed the labels to "Add".

Wave 7 PR 3 (coordinator transition, 2026-06-29) deleted the legacy
``CreateAgentModal`` and the spawn-via-tmux ``create_agent_tool_impl``
that backed it. The sole agent-creation surface is now
:class:`RegisterAgentModal`, whose trigger + dialog title both read
"Register Agent" — distinct from the original "Add" copy because the
register-only flow now hands back a snippet the operator pastes into
the user's claude config (the operator's action genuinely is "register
this agent on the backend", not just "add a row"). The "no-Deploy"
copy guard from the original review still applies.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
AGENTS_TSX = DASHBOARD / "components" / "dashboard" / "agents-dashboard.tsx"


def _read(p: Path) -> str:
    # The Agents page is a page module + a directory of satellites since
    # the <DataTablePage> migration (RegisterAgentModal now lives in
    # components/dashboard/agents/register-agent-modal.tsx); the copy
    # guards below read all of it.
    if p == AGENTS_TSX:
        from tests.dashboard_sources import agents_page_source

        return agents_page_source()
    return p.read_text(encoding="utf-8")


# ---------- RegisterAgentModal: trigger + submit copy -----------------


def test_register_agent_modal_trigger_button_says_register_agent() -> None:
    """The Agents-tab header button that opens the agent-creation
    dialog must read ``Register Agent``."""
    src = _read(AGENTS_TSX)
    assert "Register Agent" in src, (
        "RegisterAgentModal trigger button must render the label "
        "'Register Agent'."
    )
    # Bound the negative check to the dialog regions (the file
    # otherwise contains correct uses of 'deployment' in code
    # comments about path-prefix deployments).
    assert "Deploy Agent" not in src, (
        "Dashboard has resurrected the misleading 'Deploy Agent' "
        "modal copy — rename to 'Register Agent'."
    )


def test_register_agent_modal_does_not_use_deploy_submit_copy() -> None:
    """RegisterAgentModal's submit copy must not regress to the
    misleading ``Deploy`` / ``Deploying...`` strings."""
    src = _read(AGENTS_TSX)
    assert "'Deploying...'" not in src and "'Deploy'" not in src, (
        "Dashboard still uses the 'Deploy' / 'Deploying...' submit "
        "copy somewhere — the agent-creation flow does not deploy "
        "anything; rename to 'Register' / 'Registering...'."
    )


# ---------- Empty-state copy ------------------------------------------


def test_empty_state_copy_says_add_your_first() -> None:
    """The empty-state shown when no agents exist must invite the user
    to ``Add your first agent`` — not ``Deploy your first agent``."""
    src = _read(AGENTS_TSX)
    assert "Add your first agent to get started." in src, (
        "Empty-state copy must say 'Add your first agent to get "
        "started.' (was 'Deploy your first agent ...')"
    )
    assert "Deploy your first agent to get started." not in src, (
        "Empty-state still says 'Deploy your first agent ...' — "
        "rename to 'Add'."
    )
