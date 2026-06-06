"""Regression guards for the shared ``<AgentSelect>`` component
(feat/agent-select-dropdown).

Background
----------

Dennis reported that creating a task and assigning it to an agent
required the admin to *type* the agent_id into a plain ``<Input>``
(``tasks-dashboard.tsx:495-500`` pre-PR). Typo-friendly, no validation
that the agent exists, no visibility of available agents.

The Phase-1 audit also surfaced two adjacent issues:

* ``EditTaskDialog`` (``tasks-dashboard.tsx:964-988`` pre-PR) DID use a
  shadcn ``<Select>`` but sourced its agent list from
  ``apiClient.getAgents()`` — which returns every row, including
  ``status='terminated'``. Terminated agents leaked into the dropdown.
* No shared ``<AgentSelect>`` existed; each call-site reimplemented the
  dropdown by hand.

This PR introduces ``agent_mcp/dashboard/components/dashboard/shared/
agent-select.tsx`` — a single shared dropdown that filters terminated
agents via the existing ``getActiveAgents()`` store helper, pins
``Admin`` at the top, and accepts a caller-provided ``noneLabel`` prop
so task forms can render ``— Unassigned —`` while filter dropdowns
render ``— Any —``. Every agent-input site is migrated to it.

These are text-parse regression guards in the same style as
``test_dashboard_selector_helpers.py`` and
``test_dashboard_tasks_popup_polish.py`` — the dashboard ships no
jsdom/vitest, so behaviour is exercised by ``npm run build`` plus a
Firefox-MCP smoke pass against the new ``nix run .#vm-dev`` interactive
sandbox.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
AGENT_SELECT = DASHBOARD / "components/dashboard/shared/agent-select.tsx"
TASKS_TSX = DASHBOARD / "components/dashboard/tasks-dashboard.tsx"
MESSAGES_TSX = DASHBOARD / "components/dashboard/messages-dashboard.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected {p} to exist after the agent-select PR"
    return p.read_text()


# ---------- Shared component exists with the contract API -----------


def test_agent_select_component_file_exists() -> None:
    """The shared component must live at
    ``components/dashboard/shared/agent-select.tsx`` (the same
    ``shared/`` directory that already hosts ``empty-state.tsx``)."""
    assert AGENT_SELECT.exists(), (
        f"expected shared component at {AGENT_SELECT}; "
        "this is the single home for the agent dropdown — every "
        "agent-input site in the dashboard imports from here"
    )


def test_agent_select_exports_named_component() -> None:
    """``AgentSelect`` must be a named export so callers can do
    ``import { AgentSelect } from '@/components/dashboard/shared/agent-select'``."""
    src = _read(AGENT_SELECT)
    assert re.search(r"export\s+(function|const)\s+AgentSelect\b", src), (
        "AgentSelect must be a named export (function or const) from "
        "agent-select.tsx"
    )


def test_agent_select_props_contract() -> None:
    """The ``AgentSelectProps`` type must declare every field from the
    locked-design contract — ``value``, ``onChange``, ``noneLabel``,
    ``pinAdmin``, ``disabled``, ``required``, ``placeholder``."""
    src = _read(AGENT_SELECT)
    assert "AgentSelectProps" in src, (
        "AgentSelectProps type must be declared so callers have a "
        "documented contract"
    )
    for field in ("value", "onChange", "noneLabel", "pinAdmin", "disabled", "required", "placeholder"):
        # Permit either `field:` (required) or `field?:` (optional) — the
        # plan calls all of value/onChange required and the rest
        # optional, but we don't enforce optionality here. Just that the
        # field name appears at all.
        assert re.search(rf"\b{field}\??\s*:", src), (
            f"AgentSelectProps must declare a `{field}` field per the "
            f"plan's locked contract"
        )


def test_agent_select_reads_from_active_agents_store() -> None:
    """The component must source its agent list from
    ``getActiveAgents()`` on the Zustand store (the existing helper
    that already filters ``status='terminated'`` via
    ``shouldDisplayAgent``). Live-only is the locked design decision —
    task assignment to a terminated agent is meaningless."""
    src = _read(AGENT_SELECT)
    assert "getActiveAgents" in src, (
        "AgentSelect must call getActiveAgents() from data-store so the "
        "terminated-agent leak (the EditTaskDialog bug) cannot recur. "
        "Don't re-implement the filter inline; compose the existing "
        "helper."
    )
    assert "useDataStore" in src, (
        "AgentSelect must read from the useDataStore Zustand store — "
        "matches every other dashboard component that needs the live "
        "agent list"
    )


def test_agent_select_docstring_explains_none_label_convention() -> None:
    """A docstring at the top of the file must explain the caller-
    provided ``noneLabel`` convention with examples — the component
    itself stays neutral and the label text is context-specific
    (``— Unassigned —`` for task forms, ``— Any —`` for filters).
    Documenting this prevents future contributors from baking domain
    labels into the component."""
    src = _read(AGENT_SELECT)
    # Look for a docstring-style block comment somewhere in the first
    # 60 lines that mentions noneLabel and at least one concrete
    # example label.
    head = "\n".join(src.splitlines()[:60])
    assert "noneLabel" in head, (
        "the top-of-file docstring must mention `noneLabel` so future "
        "contributors understand the caller-provided-label convention"
    )
    assert ("Unassigned" in head) and ("Any" in head), (
        "the top-of-file docstring must reference the two canonical "
        "labels (`— Unassigned —` for task forms, `— Any —` for "
        "filters) so the convention is concrete, not abstract"
    )


def test_agent_select_pin_admin_default_true() -> None:
    """``pinAdmin`` defaults to ``true`` — mirror the
    messages-dashboard pre-PR pattern where Admin sat at the top of
    the From/To dropdowns. The prop exists for the rare cases where
    Admin shouldn't appear."""
    src = _read(AGENT_SELECT)
    # Look for `pinAdmin = true` or `pinAdmin: true` in a default-prop
    # assignment, or `pinAdmin ?? true`, etc.
    assert re.search(r"pinAdmin\s*[=:]\s*true", src) or re.search(
        r"pinAdmin\s*\?\?\s*true", src
    ), (
        "AgentSelect must default `pinAdmin` to true — mirror the "
        "messages-dashboard convention where Admin pins to the top"
    )


def test_agent_select_renders_admin_inline_not_in_store() -> None:
    """The component must render Admin inline via the ``pinAdmin``
    prop. Don't shove Admin into the live-agents store — Admin is
    special-cased everywhere in the codebase, and the store's job is
    to track real worker rows. Look for an ``Admin`` literal in the
    component."""
    src = _read(AGENT_SELECT)
    assert re.search(r"['\"]Admin['\"]", src), (
        "AgentSelect must render the Admin row inline (look for the "
        "string literal 'Admin'); don't rely on the store to inject it"
    )


# ---------- Migration: CreateTaskModal uses AgentSelect ------------


def test_create_task_modal_no_longer_uses_text_input_for_agent() -> None:
    """The CreateTaskModal's Assign-To affordance must NOT be a plain
    ``<Input placeholder="agent-01">`` anymore. This is the primary
    bug Dennis reported."""
    src = _read(TASKS_TSX)
    assert 'placeholder="agent-01"' not in src, (
        "CreateTaskModal still uses `<Input placeholder=\"agent-01\">` "
        "for Assign-To — Dennis's primary bug. Replace with "
        "`<AgentSelect noneLabel=\"— Unassigned —\" />`."
    )


def test_tasks_dashboard_imports_agent_select() -> None:
    """``tasks-dashboard.tsx`` must import the shared AgentSelect — it
    powers both CreateTaskModal and EditTaskDialog."""
    src = _read(TASKS_TSX)
    assert "AgentSelect" in src and "agent-select" in src, (
        "tasks-dashboard.tsx must `import { AgentSelect } from "
        "'@/components/dashboard/shared/agent-select'` — both "
        "CreateTaskModal and EditTaskDialog migrate to it"
    )


def test_tasks_dashboard_uses_unassigned_none_label() -> None:
    """Both task forms must use ``noneLabel=\"— Unassigned —\"`` — the
    label is context-specific (filters use ``— Any —``); task forms
    say ``Unassigned`` because the underlying field is nullable
    assignment, not a filter."""
    src = _read(TASKS_TSX)
    # Match noneLabel="— Unassigned —" allowing single/double quotes
    # and any whitespace flexibility around the em-dashes.
    assert re.search(r"noneLabel\s*=\s*[\"'][^\"']*Unassigned[^\"']*[\"']", src), (
        "tasks-dashboard.tsx must pass `noneLabel=\"— Unassigned —\"` "
        "to AgentSelect — task forms render the unassigned sentinel "
        "with this label per the locked plan"
    )


# ---------- Migration: EditTaskDialog uses AgentSelect (bonus fix) ---


def test_edit_task_dialog_no_longer_uses_unfiltered_get_agents() -> None:
    """EditTaskDialog used ``apiClient.getAgents().then(setAgents)``
    which returns every row including terminated agents (the
    terminated-leak bug). After this PR the local ``agents`` state +
    its fetch effect should be gone, replaced by AgentSelect reading
    from getActiveAgents()."""
    src = _read(TASKS_TSX)
    # The exact line `apiClient.getAgents().then(setAgents)` lived
    # inside an effect in EditTaskDialog at ~line 866 pre-PR.
    assert "apiClient.getAgents().then(setAgents)" not in src, (
        "EditTaskDialog still calls apiClient.getAgents().then(setAgents)"
        " — this re-introduces the terminated-agent leak. The component "
        "should let AgentSelect read live agents from the store instead "
        "of fetching its own list."
    )


# ---------- Migration: messages-dashboard filter + compose ----------


def test_messages_dashboard_imports_agent_select() -> None:
    """``messages-dashboard.tsx`` must import AgentSelect — From/To
    filter dropdowns and the Compose recipient migrate to it."""
    src = _read(MESSAGES_TSX)
    assert "AgentSelect" in src and "agent-select" in src, (
        "messages-dashboard.tsx must import { AgentSelect } from "
        "'@/components/dashboard/shared/agent-select' — From/To "
        "filters and the Compose recipient migrate to it"
    )


def test_messages_dashboard_filter_uses_any_none_label() -> None:
    """The From/To filter dropdowns must use ``noneLabel=\"— Any —\"``
    — filter semantics differ from task-form assignment semantics, so
    the label is ``Any`` (no filter) instead of ``Unassigned``."""
    src = _read(MESSAGES_TSX)
    assert re.search(r"noneLabel\s*=\s*[\"'][^\"']*Any[^\"']*[\"']", src), (
        "messages-dashboard.tsx must pass `noneLabel=\"— Any —\"` to "
        "the From/To filter AgentSelects — filter dropdowns use the "
        "`Any` label, not `Unassigned`"
    )


# ---------- nix run .#vm-dev — interactive sandbox -----------------


def test_flake_exposes_vm_dev_app() -> None:
    """``flake.nix`` must expose a new ``vm-dev`` app under
    ``apps.${system}`` so ``nix run .#vm-dev`` boots the interactive
    Path-B sandbox with a host-reachable port (18080) and a seed
    dataset (Admin + one live + one terminated agent)."""
    flake_src = Path("flake.nix").read_text()
    assert "vm-dev" in flake_src, (
        "flake.nix must declare `apps.${system}.vm-dev` so `nix run "
        ".#vm-dev` boots the interactive sandbox for Firefox-MCP "
        "smoke testing"
    )
