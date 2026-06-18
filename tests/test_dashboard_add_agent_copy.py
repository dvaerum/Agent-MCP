"""Pin the user-facing copy for the create-agent flow.

Bug surfaced by Dennis's critical review on 2026-06-17 against v5.0.48:

  The Agents tab exposed a prominent "Deploy" button + "Deploy Agent"
  modal title that, on click-through, *do not actually deploy anything*.
  ``agent_mcp/tools/admin_tools.py::create_agent_tool_impl`` calls
  ``agent_repo.create(...)`` + writes an audit log entry and returns a
  token. There is no ``subprocess``, no ``tmux launch``, no process
  spawn — the dashboard registers an agent record, period. Calling that
  flow "Deploy" misleads operators into believing a worker has been
  started; spawning the worker is a separate, manual step.

The fix renames the label sites in the create-agent flow from "Deploy"
to "Add" (verb) / "Add Agent" (button + modal title). Code-level
identifiers (function names, route names, internal handler names) are
deliberately *not* renamed — only user-visible strings change. Code
comments that refer to "path-prefix deployments" or "Standalone
deployments" of the agent-mcp service itself are also intentionally
preserved (different + correct meaning of "deploy").

The grep-style file inspection pattern matches
``test_dashboard_api_error_toast.py`` (no jsdom in this repo; behaviour
verified via ``npm run build`` plus Firefox-MCP e2e against the VM).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
AGENTS_TSX = DASHBOARD / "components" / "dashboard" / "agents-dashboard.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice_lines(src: str, start: int, end: int) -> str:
    """1-indexed inclusive line slice for region-bounded assertions."""
    return "\n".join(src.splitlines()[start - 1 : end])


# ---------- CreateAgentModal: trigger button + title + description -----


def test_create_agent_modal_trigger_button_says_add_agent() -> None:
    """The dashboard header button that opens the create-agent dialog
    must read ``Add Agent`` — pre-fix it said ``Deploy`` which was
    misleading copy (the flow only registers an agent record + token,
    it does not start a worker process)."""
    src = _read(AGENTS_TSX)
    # Bound to the CreateAgentModal region so the test doesn't trip on
    # the unrelated "path-prefix deployments" code comments further
    # down the file.
    region = _slice_lines(src, 380, 500)
    assert ">\n          Add Agent\n        </Button>" in region or (
        "Add Agent" in region and "Plus" in region
    ), (
        "CreateAgentModal trigger button must render the label "
        "'Add Agent' (was 'Deploy')"
    )
    assert ">\n          Deploy\n        </Button>" not in region, (
        "CreateAgentModal trigger button still renders the misleading "
        "label 'Deploy' — rename to 'Add Agent'"
    )


def test_create_agent_modal_title_says_add_agent() -> None:
    """The modal's ``DialogTitle`` must read ``Add Agent``. The create
    flow makes a DB row + token; there is no deployment step, so
    calling the modal "Deploy Agent" is wrong copy."""
    src = _read(AGENTS_TSX)
    region = _slice_lines(src, 380, 500)
    assert "<DialogTitle" in region and "Add Agent" in region, (
        "CreateAgentModal DialogTitle must say 'Add Agent'"
    )
    assert "Deploy Agent" not in region, (
        "CreateAgentModal DialogTitle still says 'Deploy Agent' — "
        "rename to 'Add Agent'"
    )


def test_create_agent_modal_description_does_not_say_deployment() -> None:
    """The modal description must not imply a deployment is happening.
    Pre-fix it read 'Configure a new agent for deployment.' which made
    the same false promise as the button label."""
    src = _read(AGENTS_TSX)
    region = _slice_lines(src, 380, 500)
    assert "Configure a new agent for deployment." not in region, (
        "CreateAgentModal description still claims a deployment "
        "happens — rename to make clear the worker is started "
        "separately"
    )
    assert "<DialogDescription" in region, (
        "CreateAgentModal must keep a DialogDescription for "
        "accessibility"
    )


def test_create_agent_modal_submit_button_says_add_agent() -> None:
    """The submit button inside the modal footer must read ``Add Agent``
    (and the pending state must not say 'Deploying...')."""
    src = _read(AGENTS_TSX)
    region = _slice_lines(src, 380, 500)
    assert "Add Agent" in region, (
        "CreateAgentModal submit button must say 'Add Agent'"
    )
    assert "'Deploying...'" not in region and "'Deploy'" not in region, (
        "CreateAgentModal submit button still uses the 'Deploy' / "
        "'Deploying...' copy — rename to 'Add Agent' / 'Adding...'"
    )


# ---------- Empty-state copy ------------------------------------------


def test_empty_state_copy_says_add_your_first() -> None:
    """The empty-state shown when no agents exist must invite the user
    to ``Add your first agent`` — not ``Deploy your first agent``."""
    src = _read(AGENTS_TSX)
    region = _slice_lines(src, 2000, 2050)
    assert "Add your first agent to get started." in region, (
        "Empty-state copy must say 'Add your first agent to get "
        "started.' (was 'Deploy your first agent ...')"
    )
    assert "Deploy your first agent to get started." not in region, (
        "Empty-state still says 'Deploy your first agent ...' — "
        "rename to 'Add'"
    )
