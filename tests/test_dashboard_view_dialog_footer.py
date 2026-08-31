"""Regression-guard pin: every View/Detail dialog's footer wraps
buttons instead of stacking them full-width on mobile.

Background. Four dialogs across the dashboard render a read-only
"view" of an entity with Edit/Delete/secondary actions plus Close:
`view-task-dialog.tsx`, `agent-detail-dialog.tsx` (up to 5 buttons —
Send directive / Edit / Terminate / Purge / Close), `view-message-
modal.tsx`, and `view-memory-modal.tsx`. Three of the four used the
shared `<DialogFooter>` directly, whose default
(`flex-col-reverse` below `sm:`) stacks every button full-width on a
phone — reported live: a 3-button task-detail popup ate roughly a
third of a 390x844 viewport on buttons alone. The fourth
(`view-memory-modal.tsx`) hand-rolled its own always-row footer
instead, so it never regressed the same way but also never shared the
fix.

`shared/view-dialog-footer.tsx` promotes a `<ViewDialogFooter>` that
wraps `<DialogFooter>` with a row+wrap layout, LEAVING `<DialogFooter>`
itself untouched — its stacked-by-default mobile behavior is
deliberate for confirm dialogs (Cancel-vs-Delete touch-target safety;
see `confirm-action-modal.tsx`'s own doc comment) and this fix must not
change that.

Scope is derived by filename convention (`view-*.tsx` /
`*-detail-dialog.tsx`) rather than a hardcoded list — matches every
known offender today and picks up a future dialog following the same
naming without an edit here.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path("agent_mcp/dashboard")
COMPONENTS = ROOT / "components"
DASHBOARDS = COMPONENTS / "dashboard"


def _read(p: Path) -> str:
    return p.read_text()


_SHARED_FOOTER = DASHBOARDS / "shared" / "view-dialog-footer.tsx"


def _view_dialog_files() -> list[Path]:
    found: list[Path] = []
    for pattern in ("view-*.tsx", "*-detail-dialog.tsx"):
        for f in sorted(DASHBOARDS.rglob(pattern)):
            if f.name.endswith(".test.tsx") or f == _SHARED_FOOTER:
                continue
            found.append(f)
    return sorted(set(found))


def test_view_dialogs_use_the_shared_wrapping_footer() -> None:
    """Every View/Detail dialog must render its action-button footer
    through `<ViewDialogFooter>`, not a raw `<DialogFooter>` (which
    stacks buttons full-width on mobile) or a hand-rolled footer div
    (which never gets the fix at all).
    """
    files = _view_dialog_files()
    assert files, "view-dialog audit set derived empty — the derivation is broken"

    failures: list[str] = []
    for f in files:
        src = _read(f)
        if "<ViewDialogFooter" not in src:
            failures.append(f"{f}: no <ViewDialogFooter> usage found")
    assert not failures, (
        "View/Detail dialog(s) not on the shared wrapping footer — "
        "buttons will stack full-width on mobile instead of wrapping "
        "next to each other:\n  " + "\n  ".join(failures)
    )
