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


def _read_tasks() -> str:
    return TASKS_TSX.read_text()


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
    opens the View dialog now."""
    src = _read_tasks()
    # The TableRow onClick should call openView(task) — not the
    # legacy handleTaskClick / setSelectedTask path.
    assert re.search(r"onClick=\{\(\)\s*=>\s*openView\(task\)\}", src), (
        "TableRow onClick must call openView(task) so the row body "
        "opens the View dialog (same as the eye icon)"
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
    src = _read_tasks()
    assert re.search(
        r"DeleteTaskDialog.*?DialogContent[^>]*max-w-md",
        src,
        re.DOTALL,
    ), "Delete dialog DialogContent must use max-w-md"


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
    src = _read_tasks()
    footer = _footer_block(src, "DeleteTaskDialog")
    cancel_idx = footer.find("Cancel")
    delete_idx = footer.find("Delete")
    assert cancel_idx != -1, "Delete dialog footer missing Cancel button"
    assert delete_idx != -1, "Delete dialog footer missing Delete button"
    assert cancel_idx < delete_idx, (
        "Delete dialog footer must place Cancel before the destructive "
        "Delete confirm (Cancel-left, destructive-right)"
    )


def test_delete_dialog_confirm_uses_destructive_variant() -> None:
    src = _read_tasks()
    # The destructive confirm button must use variant="destructive".
    assert re.search(
        r'DeleteTaskDialog[\s\S]*?variant="destructive"',
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
