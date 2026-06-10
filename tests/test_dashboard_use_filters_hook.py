"""Regression guards for the useFilters<T>() hook + dashboard migration.

PR 4 of the 2026-06-09 architecture review series. Candidate:
``useFilters<T>`` — single ownership of filter state across the three
dashboards that used to hand-roll the same pattern:

- ``messages-dashboard.tsx`` — 6 filter fields (from / to / type /
  priority / read / q) plus the v5.0.26 "filter changed → reset
  pagination cursor to 0" effect.
- ``tasks-dashboard.tsx`` — search / status / priority triplet.
- ``agents-dashboard.tsx`` — search / status pair.

Each dashboard previously declared its own ``useState`` for every
filter field, its own per-field updater inline at the JSX call-site,
and (in the case of messages) its own filter-watching ``useEffect``
that reset the pagination offset. The pattern was identical; the
modules didn't share it. The hook lives at
``hooks/use-filters.ts`` and exports ``useFilters<T>`` returning the
canonical shape ``{filters, setFilter, clearAll, isActive}``.

These tests are text-parse regression guards (same convention as
``test_dashboard_use_dialog_hook.py``); the fork has no jsdom
infrastructure, so behaviour is verified by ``npm run build`` +
manual click-through in the live dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The hook itself --------------------------------------


def test_use_filters_hook_file_exists() -> None:
    """``hooks/use-filters.ts`` must exist as the home of the generic hook."""
    path = DASHBOARD / "hooks" / "use-filters.ts"
    assert path.is_file(), f"expected hook at {path}"


def test_use_filters_hook_exports_function() -> None:
    """The hook must be a generic exported function ``useFilters<T>``."""
    src = (DASHBOARD / "hooks" / "use-filters.ts").read_text()
    assert re.search(r"export\s+function\s+useFilters\s*<", src), (
        "expected `export function useFilters<T>(...)` in hooks/use-filters.ts"
    )
    # Implemented on top of React's useState (the whole point is that
    # consumers stop doing it themselves).
    assert "useState" in src, "expected the hook to use React's useState internally"
    # Should also useCallback for stable setter identities (consumers
    # pass them down to memoised children).
    assert "useCallback" in src, (
        "expected the hook to use React's useCallback for stable setter identities"
    )


def test_use_filters_hook_returns_canonical_shape() -> None:
    """The hook must expose ``filters``, ``setFilter``, ``clearAll``, ``isActive``."""
    src = (DASHBOARD / "hooks" / "use-filters.ts").read_text()
    for member in ("filters", "setFilter", "clearAll", "isActive"):
        assert member in src, (
            f"expected hook to expose `{member}` on its return value"
        )


def test_use_filters_hook_accepts_on_reset_callback() -> None:
    """The hook must accept an ``onReset`` callback; messages-dashboard
    uses it to reset the pagination cursor whenever a filter changes
    (this preserves the v5.0.26 behaviour that used to live in a
    dedicated ``useEffect`` watching the filters object)."""
    src = (DASHBOARD / "hooks" / "use-filters.ts").read_text()
    assert "onReset" in src, (
        "expected hook to accept an `onReset` callback for filter-change "
        "side-effects (e.g. resetting a pagination cursor)"
    )


def test_use_filters_hook_takes_initial_filters() -> None:
    """The hook must accept an ``initial`` snapshot — it's both the
    starting state AND the target of ``clearAll``, AND the baseline
    for ``isActive``."""
    src = (DASHBOARD / "hooks" / "use-filters.ts").read_text()
    assert "initial" in src, (
        "expected hook to take `initial` (start state + clearAll target "
        "+ isActive baseline)"
    )


def test_use_filters_hook_is_active_compares_against_initial() -> None:
    """``isActive`` must be derived by comparing the live filters object
    against ``initial`` — not by tracking a separate boolean flag.
    Comment + implementation should make this explicit so future
    refactors don't accidentally invert the semantics."""
    src = (DASHBOARD / "hooks" / "use-filters.ts").read_text()
    # JSON.stringify is the documented strategy (filter shapes are
    # primitive-only across all three consumers).
    assert "JSON.stringify" in src, (
        "expected `isActive` to compare filters to initial via JSON.stringify "
        "(documented strategy for primitive-only filter shapes)"
    )


# ---------- Consumer migrations ----------------------------------


def test_messages_dashboard_imports_use_filters() -> None:
    """messages-dashboard.tsx must import the hook after migration."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "useFilters" in src, (
        "expected messages-dashboard.tsx to import useFilters after migration"
    )
    assert "use-filters" in src, (
        "expected messages-dashboard.tsx to reference '@/hooks/use-filters'"
    )


def test_tasks_dashboard_imports_use_filters() -> None:
    """tasks-dashboard.tsx must import the hook after migration."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "useFilters" in src, (
        "expected tasks-dashboard.tsx to import useFilters after migration"
    )
    assert "use-filters" in src, (
        "expected tasks-dashboard.tsx to reference '@/hooks/use-filters'"
    )


def test_agents_dashboard_imports_use_filters() -> None:
    """agents-dashboard.tsx must import the hook after migration."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    assert "useFilters" in src, (
        "expected agents-dashboard.tsx to import useFilters after migration"
    )
    assert "use-filters" in src, (
        "expected agents-dashboard.tsx to reference '@/hooks/use-filters'"
    )


# ---------- Negative assertions: legacy pattern retired ----------


def test_messages_dashboard_no_longer_hand_rolls_filter_state() -> None:
    """The legacy ``useState<Filters>(...)`` declaration must be gone
    from messages-dashboard.tsx. The hook owns the filter state now."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The legacy `useState<Filters>(...)` pattern was unique to
    # messages-dashboard.tsx; if it still appears, the migration is
    # incomplete.
    assert not re.search(r"useState\s*<\s*Filters\s*>", src), (
        "expected `useState<Filters>(...)` to be retired in favour of useFilters"
    )


def test_messages_dashboard_no_longer_has_filter_reset_effect() -> None:
    """The v5.0.26 ``useEffect(() => setCurrentOffset(0), [filters])``
    pattern must move into ``onReset`` on the hook — leaving the
    effect AND calling ``onReset`` would double-fire the reset."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The hook's onReset is the new home for "filter changed → page 1".
    # The old useEffect that watched `[filters]` and called
    # setCurrentOffset(0) must be gone.
    pattern = re.compile(
        r"useEffect\s*\(\s*\(\)\s*=>\s*\{[^}]*setCurrentOffset\s*\(\s*0\s*\)[^}]*\}\s*,\s*\[\s*filters\s*\]",
        re.DOTALL,
    )
    assert not pattern.search(src), (
        "expected the legacy `useEffect(() => setCurrentOffset(0), [filters])` "
        "to be retired — the hook's onReset callback now owns this behaviour"
    )


def test_tasks_dashboard_no_longer_declares_individual_filter_states() -> None:
    """tasks-dashboard.tsx used to declare three sibling ``useState``s:
    ``searchTerm`` / ``statusFilter`` / ``priorityFilter``. After
    migration these must come from the hook, not from individual
    ``useState`` calls."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # The three legacy state setters from the pre-migration file. All
    # three should be gone — the hook returns a single `setFilter`
    # that subsumes them.
    forbidden = [
        "const [searchTerm, setSearchTerm]",
        "const [statusFilter, setStatusFilter]",
        "const [priorityFilter, setPriorityFilter]",
    ]
    leaked = [name for name in forbidden if name in src]
    assert not leaked, (
        f"expected the legacy individual filter useStates to be retired "
        f"in favour of useFilters; still present: {leaked}"
    )


def test_agents_dashboard_no_longer_declares_individual_filter_states() -> None:
    """agents-dashboard.tsx used to declare two sibling ``useState``s:
    ``searchTerm`` / ``statusFilter``. After migration these must come
    from the hook, not from individual ``useState`` calls."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    forbidden = [
        "const [searchTerm, setSearchTerm]",
        "const [statusFilter, setStatusFilter]",
    ]
    leaked = [name for name in forbidden if name in src]
    assert not leaked, (
        f"expected the legacy individual filter useStates to be retired "
        f"in favour of useFilters; still present: {leaked}"
    )
