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

DASHBOARD = Path("agent_mcp/dashboard")
TASKS_TSX = DASHBOARD / "components/dashboard/tasks-dashboard.tsx"
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
    return TASKS_TSX.read_text()


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
    src = _read_tasks()
    assert re.search(
        r"EditTaskDialog.*?DialogContent[^>]*max-w-xl(?!\d)",
        src,
        re.DOTALL,
    ), "Edit dialog DialogContent must use max-w-xl"


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
    """Dialog body section scrolls (not the whole modal); use
    ``max-h-[80vh] overflow-y-auto`` on the body wrapper, not the
    DialogContent."""
    src = _read_tasks()
    # Either the body section or DialogContent declares max-h-[80vh]
    # (we accept either; key is overflow-y-auto is present).
    assert "max-h-[80vh]" in src, (
        "dialog body must cap height at 80vh so long tasks scroll inside "
        "the modal instead of stretching the page"
    )
    assert "overflow-y-auto" in src, (
        "dialog body must use overflow-y-auto for the scrolling area"
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
    src = _read_tasks()
    footer = _footer_block(src, "EditTaskDialog")
    cancel_idx = footer.find("Cancel")
    save_idx = footer.find("Save")
    assert cancel_idx != -1, "Edit dialog footer missing Cancel button"
    assert save_idx != -1, "Edit dialog footer missing Save button"
    assert cancel_idx < save_idx, (
        "Edit dialog footer must place Cancel before Save (Cancel-left, "
        "primary-right)"
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
    src = _read_tasks()
    # SelectTrigger inside the EditTaskDialog must declare w-full so
    # the dropdown fills its column cleanly.
    edit_block = re.search(
        r"EditTaskDialog[\s\S]*?\}\)\s*\nEditTaskDialog\.displayName",
        src,
    )
    assert edit_block, "Could not locate EditTaskDialog component body"
    body = edit_block.group(0)
    assert "w-full" in body, (
        "Edit dialog SelectTrigger/Input must use w-full so controls "
        "fill their column"
    )


# ---------- Monospace task_id footer in View dialog ---------------


def test_view_dialog_task_id_uses_monospace() -> None:
    src = _read_tasks()
    view_block = re.search(
        r"ViewTaskDialog[\s\S]*?\}\)\s*\nViewTaskDialog\.displayName",
        src,
    )
    assert view_block, "Could not locate ViewTaskDialog component body"
    body = view_block.group(0)
    # task_id must be rendered with font-mono.
    assert "font-mono" in body, (
        "View dialog must render task_id (and other code-like fields) "
        "in font-mono"
    )
    assert "task.task_id" in body, "View dialog must surface task.task_id"
