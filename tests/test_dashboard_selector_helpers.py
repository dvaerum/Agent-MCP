"""Regression guards for PR-W1d (architecture deepening, Finding #7):
extracted ``normalizeAgentId`` and ``selectTasks`` helpers in the
dashboard's Zustand data-store.

Background
----------

Three selectors in ``agent_mcp/dashboard/lib/stores/data-store.ts`` --
``getAgentTasks``, ``getAgentActions``, ``getAgentTaskAnalysis`` --
each duplicated the same Admin/admin/strip-prefix dance plus the same
``t.assigned_to === ...`` predicate inline. PR #130 fixed an overcount
bug ("completed tasks shown as assigned") on the Agents row but the
fix only landed in ``agents-dashboard.tsx``; the same buggy filter
pattern in ``data-store.ts`` was unchanged. This PR refactors the
three selectors to compose two helpers so the next bug-fix can be
applied in one place.

The dashboard ships no jsdom / vitest, so these are text-parse guards
in the same style as ``test_dashboard_assigned_excludes_terminal_tasks.py``
and ``test_dashboard_agents_popup_polish.py``. End-to-end behaviour is
verified by ``npm run build`` (clean) and a Firefox-MCP smoke pass
documented on the PR.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
DATA_STORE = DASHBOARD / "lib/stores/data-store.ts"
SELECTORS = DASHBOARD / "lib/stores/selectors.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"expected {p} to exist after PR-W1d"
    return p.read_text()


# ---------- The helpers exist as named exports ------------------------


def test_normalize_agent_id_helper_exists() -> None:
    """``normalizeAgentId(agentId: string): string`` must exist as a
    named export so the Admin/admin/strip-prefix dance has exactly one
    home. We accept the export in either ``data-store.ts`` or its
    sibling ``selectors.ts`` (the spec leaves the file split as an
    implementation choice)."""
    sources = []
    if SELECTORS.exists():
        sources.append(_read(SELECTORS))
    sources.append(_read(DATA_STORE))
    blob = "\n".join(sources)
    assert "export function normalizeAgentId" in blob, (
        "expected `export function normalizeAgentId(agentId: string): string` "
        "in lib/stores/data-store.ts or lib/stores/selectors.ts"
    )


def test_select_tasks_helper_exists() -> None:
    """``selectTasks(tasks, criteria)`` must exist as a named export.
    Same file flexibility as the helper above."""
    sources = []
    if SELECTORS.exists():
        sources.append(_read(SELECTORS))
    sources.append(_read(DATA_STORE))
    blob = "\n".join(sources)
    assert "export function selectTasks" in blob, (
        "expected `export function selectTasks(tasks, criteria)` in "
        "lib/stores/data-store.ts or lib/stores/selectors.ts"
    )


def test_task_criteria_type_exists() -> None:
    """The ``TaskCriteria`` type/interface must exist so callers have a
    documented contract for the composable filter. ``assignedTo``,
    ``statusIn``, ``statusNotIn`` are the criteria the spec calls
    out."""
    sources = []
    if SELECTORS.exists():
        sources.append(_read(SELECTORS))
    sources.append(_read(DATA_STORE))
    blob = "\n".join(sources)
    assert "TaskCriteria" in blob, (
        "expected a `TaskCriteria` type/interface declaring the composable "
        "filter shape — search lib/stores/{data-store,selectors}.ts"
    )
    for key in ("assignedTo", "statusIn", "statusNotIn"):
        assert key in blob, (
            f"TaskCriteria must declare a `{key}` field — selectors that "
            f"want to exclude terminal statuses (PR #130 fix) need "
            f"statusNotIn to compose, and assignedTo is the primary "
            f"axis of the three callers."
        )


# ---------- The selectors compose, not duplicate ---------------------


def test_data_store_no_longer_duplicates_admin_normalize() -> None:
    """The signature-Admin-pair predicate ``(normalizedAgentId === 'admin'
    && (t.assigned_to === 'Admin' || t.assigned_to === 'admin'))`` was
    copy-pasted into three places in ``data-store.ts``. After PR-W1d
    that exact phrasing must not appear inline in the file — the
    selectors must compose ``normalizeAgentId`` + ``selectTasks``
    instead. (The helper itself may use a similar predicate; we just
    forbid the duplication in the call sites.)"""
    src = _read(DATA_STORE)
    # Tolerant regex: any whitespace between tokens.
    pattern = re.compile(
        r"normalizedAgentId\s*===\s*['\"]admin['\"]\s*&&\s*\(\s*t\.assigned_to\s*===\s*['\"]Admin['\"]\s*\|\|\s*t\.assigned_to\s*===\s*['\"]admin['\"]",
    )
    matches = pattern.findall(src)
    assert not matches, (
        "data-store.ts still contains the inline Admin/admin assigned_to "
        "predicate -- it should compose normalizeAgentId + selectTasks "
        "from the helpers instead. Found {n} match(es).".format(n=len(matches))
    )


def test_data_store_selectors_call_helpers() -> None:
    """``getAgentTasks``, ``getAgentActions``, ``getAgentTaskAnalysis``
    must each reference at least one of the new helpers (proves they
    were actually refactored, not just left alongside dead helpers).
    """
    src = _read(DATA_STORE)
    # Scope the search to the create() body so we don't accidentally
    # match the TypeScript interface declaration at the top of the
    # file.
    impl_start = src.find("create<DataStore>")
    assert impl_start != -1, "expected create<DataStore>(...) in data-store.ts"
    impl_src = src[impl_start:]
    for selector in ("getAgentTasks", "getAgentActions", "getAgentTaskAnalysis"):
        # Implementation form: `selector: (agentId: string) => {` with
        # a function body brace (the interface uses `=> ReturnType`
        # without an opening body brace, or an inline object literal
        # for the return type -- but that's in the type-only block
        # above `create<DataStore>`).
        m = re.search(rf"{selector}\s*:\s*\([^)]*\)\s*=>\s*\{{", impl_src)
        assert m is not None, f"selector {selector} implementation not found in data-store.ts"
        # The selector body spans until the next selector definition or
        # closing of the create() block. A 1500-char window is enough
        # to cover the largest of the three.
        body = impl_src[m.start(): m.start() + 1500]
        assert (
            "normalizeAgentId" in body or "selectTasks" in body
        ), (
            f"{selector} body does not reference normalizeAgentId or "
            f"selectTasks — was the selector actually migrated to compose "
            f"the helpers? Body window:\n{body[:400]}..."
        )


# ---------- PR #130 fix is preserved when composed -------------------


def test_data_store_excludes_terminal_when_selectors_mean_open() -> None:
    """The agents-dashboard row-pill fix from PR #130 must still hold
    after the data-store refactor: any selector body that filters by
    ``assigned_to`` AND is intended to be a 'currently to-do' list
    must exclude terminal statuses. The simplest invariant we can
    assert: the data-store source mentions all three terminal status
    strings ('completed', 'cancelled', 'failed') at least once each.
    Either as a default-statusNotIn list inside ``selectTasks``, or as
    an explicit pass-through inside ``getAgentTaskAnalysis``'s
    assignedTasks branch.

    (The row-pill regression in ``agents-dashboard.tsx`` is covered by
    test_dashboard_assigned_excludes_terminal_tasks.py and stays as-is.)
    """
    sources = [_read(DATA_STORE)]
    if SELECTORS.exists():
        sources.append(_read(SELECTORS))
    blob = "\n".join(sources)
    for status in ("completed", "cancelled", "failed"):
        assert f"'{status}'" in blob or f'"{status}"' in blob, (
            f"expected terminal status {status!r} to be referenced in "
            f"lib/stores/{{data-store,selectors}}.ts so the dashboard's "
            f"PR #130 fix (open-tasks-only) composes through the new "
            f"selectTasks helper"
        )
