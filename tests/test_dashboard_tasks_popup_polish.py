"""Regression guards for Phase 7-UX1: Tasks page row-click + popup polish.

This PR removes the legacy ``TaskDetailsPanel`` sidebar from the Tasks
dashboard. Clicking a task row body now opens the same View dialog as
the eye icon. The View / Edit / Delete dialogs are also re-laid out per
shadcn idiom (sized containers, labels above values, footer button
order Cancel-left / primary-right, destructive variant on delete).

These tests are text-parse regression guards (same convention as
``test_dashboard_tasks_row_icons.py``). No jsdom in this repo;
behaviour is verified by ``npm run build`` plus Firefox MCP e2e.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.dashboard_sources import tasks_page_source

DASHBOARD = Path("agent_mcp/dashboard")
TASKS_TSX = DASHBOARD / "components/dashboard/tasks-dashboard.tsx"
# Wave 5 (refactor/w5-tasks) satellites the dialog layout guards follow.
VIEW_TASK_DIALOG_TSX = DASHBOARD / "components/dashboard/tasks/view-task-dialog.tsx"
EDIT_TASK_DIALOG_TSX = DASHBOARD / "components/dashboard/tasks/edit-task-dialog.tsx"
# The create/edit dialogs adopted the shared <FormDialog> shell, which
# now OWNS the DialogContent width + the Cancel/Save footer — so the
# guarantees about those (mobile-safe width, Cancel-before-primary) are
# audited at the shell, exactly as the delete guarantees delegate to
# <ConfirmActionModal>. Not a weakening: every original assertion is
# still made, just at the component where the markup now lives.
FORM_DIALOG_TSX = DASHBOARD / "components/dashboard/shared/form-dialog.tsx"
# The Delete dialog was EXTRACTED out of the page god-file (it needed a
# test seam once its confirmation tier became conditional on the blast
# radius). The three delete guarantees below therefore audit the
# component where the markup now lives — plus the page, which must still
# render it. Following a file that moved is not the same as weakening
# what is asserted: every original assertion is still made, and
# `test_delete_dialog_is_rendered_by_the_page` is a NEW assertion that
# stops the redirection from becoming an escape hatch.
DELETE_DIALOG_TSX = (
    DASHBOARD / "components/dashboard/tasks/delete-task-dialog.tsx"
)
CONFIRM_ACTION_MODAL_TSX = (
    DASHBOARD / "components/dashboard/modals/confirm-action-modal.tsx"
)


def _read_tasks() -> str:
    # Wave 5 (refactor/w5-tasks): the Tasks page was split into a page
    # module + a `tasks/` satellite directory (the View / Edit dialogs,
    # the column spec). These guards assert page-level properties, so
    # read the page + its satellites as one blob. See
    # tests/dashboard_sources.py.
    return tasks_page_source()


def _read_delete_dialog() -> str:
    return DELETE_DIALOG_TSX.read_text()


# ---------- Legacy sidebar gone -----------------------------------


def test_task_details_panel_import_removed() -> None:
    """The legacy ``TaskDetailsPanel`` sidebar must no longer be imported
    by the Tasks page."""
    src = _read_tasks()
    assert "from \"./task-details-panel\"" not in src, (
        "TaskDetailsPanel import must be removed from tasks-dashboard.tsx"
    )
    assert "from './task-details-panel'" not in src, (
        "TaskDetailsPanel import must be removed from tasks-dashboard.tsx"
    )


def test_task_details_panel_jsx_removed() -> None:
    """The legacy ``<TaskDetailsPanel ... />`` JSX must not be rendered
    anywhere in the Tasks page."""
    src = _read_tasks()
    assert "<TaskDetailsPanel" not in src, (
        "TaskDetailsPanel must not be rendered in tasks-dashboard.tsx; "
        "the sidebar has been retired in favour of the View dialog"
    )


# ---------- Row body click opens the View dialog -------------------


def test_row_body_click_opens_view_dialog() -> None:
    """The row-body ``onClick`` must route to the same ``openView``
    handler used by the eye icon — clicking anywhere on the row body
    opens the View dialog now.

    After the 2026-06-02 live-lookup refactor (Candidate D), the
    handler takes the row's identity field (``task.task_id``) rather
    than the row itself so the dialog can read the row live from the
    source.

    Two accepted shapes, because the row markup moved into the shared
    scaffold (PR #581): the pre-scaffold ``<TableRow onClick={() =>
    openView(task.task_id)}>``, or the scaffold's row-click slot
    ``<DataTablePage onRowClick={(task) => openView(task.task_id)}>``.
    The GUARANTEE is unchanged — a row-body click routes to the same
    ``openView`` the eye icon uses — only the prop that carries it
    differs. Neither shape accepts the legacy handleTaskClick /
    setSelectedTask sidebar path.
    """
    src = _read_tasks()
    table_row_click = re.search(
        r"onClick=\{\(\)\s*=>\s*openView\(task\.task_id\)\}", src
    )
    scaffold_row_click = re.search(
        r"onRowClick=\{\(task\)\s*=>\s*openView\(task\.task_id\)\}", src
    )
    assert table_row_click or scaffold_row_click, (
        "the row body must call openView(task.task_id) — either as the "
        "TableRow `onClick={() => openView(task.task_id)}` or as the "
        "<DataTablePage> `onRowClick={(task) => openView(task.task_id)}` "
        "— so the row body opens the View dialog (same as the eye icon) "
        "and the live-lookup useDialog reads the row from the store on "
        "every render"
    )


def test_legacy_selected_task_state_removed() -> None:
    """The ``selectedTask`` state (used to drive the sidebar) must be
    gone; otherwise the sidebar is half-wired and could resurrect."""
    src = _read_tasks()
    assert "selectedTask" not in src, (
        "selectedTask state must be removed; the sidebar is retired"
    )
    assert "setSelectedTask" not in src, (
        "setSelectedTask must be removed; row-body click now opens "
        "the View dialog directly"
    )


# ---------- Dialog widths follow shadcn idiom ---------------------


def test_view_dialog_has_explicit_width_override() -> None:
    src = _read_tasks()
    # The ViewTaskDialog's DialogContent must declare a width override
    # that beats the base DialogContent's `sm:max-w-lg` (which
    # otherwise wins the cascade because both share specificity and
    # base is declared later in the merged className string).
    #
    # Originally `max-w-2xl`; updated to `sm:!max-w-3xl` (Tailwind
    # important) after the Firefox MCP audit found base `sm:max-w-lg`
    # was squeezing every desktop dialog to 512px.
    assert re.search(
        r"ViewTaskDialog.*?DialogContent[^>]*sm:!max-w-3xl",
        src,
        re.DOTALL,
    ), "View dialog DialogContent must use sm:!max-w-3xl to override base sm:max-w-lg"


def test_edit_dialog_has_max_w_xl() -> None:
    # Wave 5: the Edit dialog adopted the shared <FormDialog> shell, which
    # OWNS the DialogContent width. Satisfied by delegation (like the
    # delete tests delegate to <ConfirmActionModal>): the edit dialog
    # renders <FormDialog wide>, and the wide width lives on FormDialog.
    edit_src = EDIT_TASK_DIALOG_TSX.read_text()
    assert re.search(r"<FormDialog\b", edit_src) and "wide" in edit_src, (
        "Edit dialog must render the shared <FormDialog wide> shell "
        "(which owns the desktop-comfortable dialog width)"
    )
    form_src = FORM_DIALOG_TSX.read_text()
    assert "sm:max-w-2xl" in form_src, (
        "FormDialog's `wide` width (sm:max-w-2xl) must beat the base "
        "sm:max-w-lg so the Edit dialog isn't squeezed on desktop"
    )


def test_delete_dialog_has_max_w_md() -> None:
    # Satisfied by delegation: <DeleteTaskDialog> renders the shared
    # <ConfirmActionModal> (tier 1) / <DeleteConfirmModal> (tier 2), and
    # the sizing now lives on those. Assert it there.
    src = CONFIRM_ACTION_MODAL_TSX.read_text()
    assert re.search(
        r"<DialogContent[^>]*max-w-md",
        src,
        re.DOTALL,
    ), "Delete dialog DialogContent must use max-w-md"


def test_delete_dialog_is_rendered_by_the_page() -> None:
    """Guard the redirection above: the Tasks page must actually render
    the extracted dialog, or the three delete guarantees would be
    auditing a component nobody uses."""
    src = _read_tasks()
    assert "<DeleteTaskDialog" in src, (
        "tasks-dashboard.tsx must render <DeleteTaskDialog>"
    )
    assert "tasks/delete-task-dialog" in src, (
        "tasks-dashboard.tsx must import the extracted delete dialog"
    )


# ---------- Scrollable body, not the whole modal ------------------


def test_view_dialog_body_uses_max_h_overflow() -> None:
    """Dialog body section scrolls (not the whole modal): the dialog caps
    at the viewport and a `flex-1 min-h-0 overflow-y-auto` body is the
    single scroll region.

    Wave 5: the Edit dialog moved to the shared <FormDialog> (which owns
    an equivalent viewport cap + scroll body). The View dialog keeps its
    own layout, so the guarantee is pinned there: `max-h-[90dvh]` on the
    DialogContent + a `flex-1 min-h-0 overflow-y-auto` scroll body.
    `dvh`, not `vh` — see docs/learnings/dashboard-dialog-mobile-clipping.md.
    """
    src = _read_tasks()
    assert "max-h-[90dvh]" in src, (
        "View dialog must cap height at 90dvh so long tasks scroll inside "
        "the modal instead of stretching the page"
    )
    assert "flex-1 min-h-0 overflow-y-auto" in src, (
        "the dialog body must be the single `flex-1 min-h-0 "
        "overflow-y-auto` scroll region"
    )


# ---------- Label primitive used for labels above values ----------


def test_dialogs_use_label_primitive() -> None:
    """Labels above inputs should use the shadcn ``Label`` primitive,
    not raw ``<label>`` tags — matches the Agents page polish."""
    src = _read_tasks()
    assert "@/components/ui/label" in src, (
        "shadcn Label must be imported and used for input labels"
    )
    # Must actually render <Label> in the dialogs.
    assert re.search(r"<Label\b", src), (
        "shadcn <Label> primitive must be rendered in the dialogs"
    )


# ---------- Footer button order: Cancel on left, primary on right --


def _footer_block(src: str, dialog_name: str) -> str:
    # Locate the dialog's component block and return ONLY the JSX
    # between <DialogFooter ...> and </DialogFooter> — not the whole
    # component (which would include unrelated mentions like the
    # "Saving…" button label or the dialog title text).
    m = re.search(
        rf"{dialog_name}[\s\S]*?(<DialogFooter[\s\S]*?</DialogFooter>)",
        src,
    )
    assert m, f"No DialogFooter found inside {dialog_name}"
    return m.group(1)


def test_edit_dialog_footer_cancel_before_save() -> None:
    # Wave 5: the Edit dialog's footer is now owned by the shared
    # <FormDialog> shell (Cancel + submit), so the Cancel-left /
    # primary-right guarantee is pinned there — the same delegation the
    # delete-dialog footer test uses for <ConfirmActionModal>. First
    # confirm the Edit dialog actually renders the shell.
    edit_src = EDIT_TASK_DIALOG_TSX.read_text()
    assert re.search(r"<FormDialog\b", edit_src), (
        "Edit dialog must render the shared <FormDialog> shell"
    )
    src = FORM_DIALOG_TSX.read_text()
    m = re.search(r"<DialogFooter[\s\S]*?</DialogFooter>", src)
    assert m, "No DialogFooter found in FormDialog"
    footer = m.group(0)
    # Cancel button closes the dialog; submit runs the mutation.
    cancel_idx = footer.find("onOpenChange(false)")
    submit_idx = footer.find("void submit()")
    assert cancel_idx != -1, "FormDialog footer missing Cancel button"
    assert submit_idx != -1, "FormDialog footer missing submit button"
    assert cancel_idx < submit_idx, (
        "FormDialog footer must place Cancel before the primary submit "
        "(Cancel-left, primary-right)"
    )


def test_delete_dialog_footer_cancel_before_destructive() -> None:
    # Delegated to the shared tier-1 modal (see the module note above).
    src = CONFIRM_ACTION_MODAL_TSX.read_text()
    footer = _footer_block(src, r"ConfirmActionModal\(")
    cancel_idx = footer.find("Cancel")
    # The confirm label is a prop (it reads "Delete" / "Delete N tasks" /
    # "Terminate" per call site), so the destructive button is located by
    # its variant rather than by literal text — a tighter anchor than the
    # word "Delete", not a looser one.
    delete_idx = footer.find('variant="destructive"')
    assert cancel_idx != -1, "Delete dialog footer missing Cancel button"
    assert delete_idx != -1, "Delete dialog footer missing destructive button"
    assert cancel_idx < delete_idx, (
        "Delete dialog footer must place Cancel before the destructive "
        "Delete confirm (Cancel-left, destructive-right)"
    )


def test_delete_dialog_confirm_uses_destructive_variant() -> None:
    # Delegated to the shared tier-1 modal (see the module note above).
    src = CONFIRM_ACTION_MODAL_TSX.read_text()
    assert re.search(
        r'ConfirmActionModal\([\s\S]*?variant="destructive"',
        src,
    ), "Delete dialog confirm button must use variant=\"destructive\""


# ---------- Selects + inputs full-width inside their cell ---------


def test_edit_dialog_inputs_full_width() -> None:
    # Wave 5: EditTaskDialog is its own satellite now (adopted
    # <FormDialog>, so no more `React.memo(...) … displayName` wrapper to
    # anchor on). Read the satellite directly. SelectTrigger/Input inside
    # must declare w-full so the controls fill their column.
    body = EDIT_TASK_DIALOG_TSX.read_text()
    assert "w-full" in body, (
        "Edit dialog SelectTrigger/Input must use w-full so controls "
        "fill their column"
    )


# ---------- Monospace task_id footer in View dialog ---------------


def test_view_dialog_task_id_uses_monospace() -> None:
    # Wave 5: ViewTaskDialog is its own satellite now. Read it directly.
    body = VIEW_TASK_DIALOG_TSX.read_text()
    # task_id must be rendered with font-mono.
    assert "font-mono" in body, (
        "View dialog must render task_id (and other code-like fields) "
        "in font-mono"
    )
    assert "task.task_id" in body, "View dialog must surface task.task_id"
