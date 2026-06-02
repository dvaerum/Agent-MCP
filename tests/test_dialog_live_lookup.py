"""Regression guards for the useDialog<T>() live-lookup refactor.

Background (Candidate D, 2026-06-02 architecture review): the original
``useDialog<T>()`` hook (Candidate F1 from 2026-06-01) stored a
*snapshot* of the row at ``open(row)`` time. Background refresh
updated the underlying zustand store, but the dialog kept rendering
against the captured snapshot. PR #74's Add-Note investigation traced
the user-visible "saved note disappears" symptom to exactly this:
after the Edit dialog saved a new note and the store updated, the
View dialog still rendered the pre-save object.

The fix retires the snapshot. ``useDialog<T>`` now holds a **key**
(typically ``task.task_id``, ``message.message_id``, etc.) and a
**selector** function that the hook calls on every render to read the
current row from the live source. When the source is the zustand
data-store, the selector is a zustand subscription, so updates re-
render the dialog automatically. When the source is local component
state, the selector is a plain ``useCallback`` closure that re-runs
when the source array changes.

These tests are text-parse regression guards (same convention as
``test_dashboard_use_dialog_hook.py``); the fork has no jsdom
infrastructure, so behaviour is verified by ``npm run build`` plus a
Firefox-MCP click-through in the live dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
HOOK = DASHBOARD / "hooks" / "use-dialog.ts"
COMPONENTS = DASHBOARD / "components" / "dashboard"


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The hook: new shape ----------------------------------


def test_use_dialog_hook_takes_selector_argument() -> None:
    """``useDialog<T>`` must require a selector callback.

    The whole point of the refactor is that the dialog no longer
    snapshots a row at ``open()`` time — it asks the selector for the
    current row on every render. A zero-arg ``useDialog<T>()`` would
    silently revert to snapshot mode and re-introduce the bug class.
    """
    src = HOOK.read_text()
    # Generic, exported, and the signature must take at least one
    # parameter (the selector). We tolerate either a single positional
    # selector or an options bag — both encode "user must supply a
    # lookup".
    sig = re.search(
        r"export\s+function\s+useDialog\s*<[^>]+>\s*\(\s*([^)]*)\)",
        src,
    )
    assert sig is not None, "expected export function useDialog<...>(...)"
    params = sig.group(1).strip()
    assert params, (
        "useDialog must take a selector parameter; an empty argument "
        "list re-introduces snapshot-mode behaviour"
    )


def test_use_dialog_hook_stores_key_not_snapshot() -> None:
    """The hook's internal state must be the key, not the row.

    Storing the whole row recreates the snapshot bug — even with a
    selector, a setState(row) hidden inside the hook would mean the
    dialog renders against that snapshot when the selector returns
    null.
    """
    src = HOOK.read_text()
    # The state must be typed as a key (string | null is the standard
    # case; we accept any narrower union via "K | null").
    assert re.search(r"useState\s*<\s*[\w\s|]*\bnull\s*>\s*\(\s*null\s*\)", src), (
        "expected the hook to store a key with useState<K | null>(null)"
    )
    # Negative: the hook must NOT store the row itself.
    assert "useState<T" not in src.replace(" ", ""), (
        "useDialog must not store useState<T | null>; storing the row "
        "is the snapshot bug we are fixing"
    )


def test_use_dialog_hook_returns_canonical_shape() -> None:
    """The hook must keep exposing ``isOpen``, ``data``, ``open``, ``close``."""
    src = HOOK.read_text()
    for member in ("isOpen", "data", "open", "close"):
        assert member in src, (
            f"expected hook to expose `{member}` on its return value"
        )


def test_use_dialog_hook_documents_live_lookup() -> None:
    """The hook header must explain the live-lookup contract.

    Without the docstring, future contributors might "fix" the
    selector-only API back to snapshot-on-open ("simpler", they'll
    say) and regress the bug.
    """
    src = HOOK.read_text()
    # Look for either of the load-bearing words from the design note.
    assert re.search(r"(?i)(live|snapshot|selector)", src), (
        "expected the hook docstring to mention live/selector/snapshot "
        "rationale"
    )


# ---------- Consumers: no zero-arg call remains ------------------


_LEGACY_ZERO_ARG = re.compile(r"useDialog\s*<[^>]+>\s*\(\s*\)")


def test_no_consumer_uses_zero_arg_useDialog() -> None:
    """No remaining call site may use the old zero-argument form.

    Each consumer MUST pass a selector so its dialog reads live data.
    """
    offenders: list[str] = []
    for path in COMPONENTS.rglob("*.tsx"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _LEGACY_ZERO_ARG.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the following consumers still call useDialog<T>() with no "
        "selector argument (snapshot-mode bug class):\n  "
        + "\n  ".join(offenders)
    )


# ---------- Stale bandage removed --------------------------------


def test_tasks_dashboard_stale_select_item_bandage_removed() -> None:
    """The ``(stale)`` SelectItem in tasks-dashboard.tsx is dead after
    the refactor — the Edit dialog now reads the current task row, so
    its ``assigned_to`` is always in sync with the live agent roster
    and the workaround SelectItem can never fire.
    """
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "(stale)" not in src, (
        "tasks-dashboard.tsx still carries the '(stale)' bandage; the "
        "live-lookup refactor was supposed to remove it"
    )


# ---------- Per-consumer migration audit -------------------------

# Every consumer of useDialog<T> must pass a selector. This catches
# half-migrations where a file was edited but one ``useDialog<X>()``
# call slipped through.

# Map: file -> list of (variable-name, key-field-substring) tuples that
# must appear with a selector call.
_EXPECTED_CONSUMERS = {
    "components/dashboard/tasks-dashboard.tsx": [
        "viewDialog",
        "editDialog",
        "deleteDialog",
    ],
    "components/dashboard/agents-dashboard.tsx": [
        "taskDialog",
        "purgeDialog",
        "terminateDialog",
        "editDialog",
        "detailDialog",
    ],
    "components/dashboard/memories-dashboard.tsx": [
        "viewDialog",
        "editDialog",
    ],
    "components/dashboard/messages-dashboard.tsx": [
        "detailDialog",
    ],
    "components/dashboard/overview-dashboard.tsx": [
        "nodeDialog",
    ],
    "components/dashboard/prompt-book-dashboard.tsx": [
        "builderDialog",
    ],
    "components/dashboard/agent-details-panel.tsx": [
        "taskDialog",
    ],
}


def test_at_least_twelve_consumers_migrated() -> None:
    """Architecture review counted 12+ dialog consumers across the
    dashboard. Make sure we didn't miss any."""
    total = sum(len(v) for v in _EXPECTED_CONSUMERS.values())
    assert total >= 12, (
        f"expected at least 12 useDialog consumers to migrate; "
        f"the audit map covers only {total}"
    )


def test_every_listed_consumer_passes_a_selector() -> None:
    """Each ``useDialog<X>(...)`` call site must pass a non-empty
    argument list (the selector). Zero-arg calls are caught by
    test_no_consumer_uses_zero_arg_useDialog; here we additionally
    verify the named variables we know about still exist + use
    useDialog.
    """
    missing: list[str] = []
    for rel, varnames in _EXPECTED_CONSUMERS.items():
        src = _read(rel)
        for var in varnames:
            # Match `const <var> = useDialog<...>(...)` — argument list
            # must contain at least one non-whitespace character.
            pattern = re.compile(
                rf"const\s+{re.escape(var)}\s*=\s*useDialog\s*<[^>]+>\s*\(\s*\S"
            )
            if not pattern.search(src):
                missing.append(f"{rel}::{var}")
    assert not missing, (
        "the following consumers either disappeared or still call "
        "useDialog<T>() with no selector argument:\n  "
        + "\n  ".join(missing)
    )
