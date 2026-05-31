"""Regression guards for the useDialog<T>() hook + dashboard migration.

Candidate F1 from the 2026-06-01 architecture review: ~12 dashboard
dialogs each maintained their own ad-hoc state machine, typically the
pair ``useState<boolean>(false)`` for "open" plus ``useState<T |
null>(null)`` for the row being viewed/edited/deleted. That triplet
of ``open`` / ``data`` / ``setOpen+setData`` plumbing was repeated
across tasks-, agents-, memories-, messages-, and prompt-book-
dashboard, each with its own ad-hoc naming. Candidate F1 collapses
the duplication behind a single generic hook
``hooks/use-dialog.ts::useDialog<T>()`` that returns
``{isOpen, data, open, close}``.

These tests are text-parse regression guards (same convention as
test_dashboard_messages_detail_popup.py / test_dashboard_tasks_row_icons.py);
the fork has no jsdom infrastructure, so behaviour is verified by
``npm run build`` + manual click-through in the live dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The hook itself --------------------------------------


def test_use_dialog_hook_file_exists() -> None:
    """``hooks/use-dialog.ts`` must exist as the home of the generic hook."""
    path = DASHBOARD / "hooks" / "use-dialog.ts"
    assert path.is_file(), f"expected hook at {path}"


def test_use_dialog_hook_exports_function() -> None:
    """The hook must be a generic exported function ``useDialog<T>``."""
    src = (DASHBOARD / "hooks" / "use-dialog.ts").read_text()
    # Generic, exported, named useDialog.
    assert re.search(r"export\s+function\s+useDialog\s*<", src), (
        "expected `export function useDialog<T>(...)` in hooks/use-dialog.ts"
    )
    # Must be implemented on top of React's useState (the whole point
    # is that consumers stop doing it themselves).
    assert "useState" in src, "expected the hook to use React's useState internally"


def test_use_dialog_hook_returns_canonical_shape() -> None:
    """The hook must expose ``isOpen``, ``data``, ``open``, ``close``."""
    src = (DASHBOARD / "hooks" / "use-dialog.ts").read_text()
    for member in ("isOpen", "data", "open", "close"):
        assert member in src, (
            f"expected hook to expose `{member}` on its return value"
        )


# ---------- At least one consumer imports the hook ---------------


def test_at_least_one_consumer_imports_use_dialog() -> None:
    """Some dashboard component under components/dashboard/ must import
    the hook — proving the migration started, not just the file."""
    components = DASHBOARD / "components" / "dashboard"
    importers = [
        p
        for p in components.rglob("*.tsx")
        if "useDialog" in p.read_text() and "use-dialog" in p.read_text()
    ]
    assert importers, (
        "expected at least one component under components/dashboard/ to "
        "import useDialog from '@/hooks/use-dialog'"
    )


# ---------- Negative assertion: legacy pair retired --------------

# We pick agents-dashboard.tsx as the canary because it had the
# heaviest concentration of the legacy pair (5 separate dialogs each
# with its own bool+null pair: detail / edit / terminate / purge /
# task). After migration the entire ``useState<boolean>(false)`` line
# pattern (paired with a sibling ``useState<X | null>(null)``) must
# be gone from this file. Other migrated files are spot-checked in
# their own PRs.

_LEGACY_BOOL_LINE = re.compile(
    r"useState\s*<\s*boolean\s*>\s*\(\s*false\s*\)|useState\s*\(\s*false\s*\)"
)
_LEGACY_NULL_LINE = re.compile(r"useState\s*<\s*\w[\w<>,\s|']*\s*\|\s*null\s*>\s*\(\s*null\s*\)")


def test_agents_dashboard_no_longer_has_legacy_dialog_pair() -> None:
    """agents-dashboard.tsx used to declare 5 ``setXxxDialogOpen`` boolean
    flags alongside the corresponding ``setSelectedXxx`` nullable row
    holders. The migration retires every one of them."""
    src = _read("components/dashboard/agents-dashboard.tsx")
    # The five tell-tale state-setter names from the pre-migration file.
    forbidden = [
        "setTaskDialogOpen",
        "setPurgeDialogOpen",
        "setTerminateDialogOpen",
        "setEditDialogOpen",
        "setDetailDialogOpen",
    ]
    leaked = [name for name in forbidden if name in src]
    assert not leaked, (
        f"expected the legacy ``setXxxDialogOpen`` boolean setters to be "
        f"retired in favour of useDialog; still present: {leaked}"
    )
    # And the hook itself must be present — i.e. we didn't just delete
    # the state and leave the dialogs floating.
    assert "useDialog" in src, (
        "expected agents-dashboard.tsx to use useDialog after migration"
    )
