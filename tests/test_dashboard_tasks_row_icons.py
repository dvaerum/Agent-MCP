"""Regression guards for the dashboard Tasks page row-action icons.

Before this PR every row icon on the Tasks page eventually opened the
same sidebar (TaskDetailsPanel) — the three buttons did the same
thing. This PR splits the row into three distinct icon actions, each
backed by a Dialog modal (NOT the sidebar):

- Eye    -> read-only "View" Dialog showing every task field.
- Pencil -> "Edit" Dialog letting an admin mutate the task fields and
            saving via POST /api/update-task-dashboard (+ assigned_to
            via the same endpoint, extended in this PR).
- Trash2 -> Delete confirm Dialog, then DELETE /api/tasks/<id>
            (PR #12) with the admin token.

Text-parse regression guards (same convention as
test_dashboard_messages_detail_popup.py and
test_dashboard_agent_restore_purge.py); we don't have jsdom in this
repo and behaviour is verified by `npm run build` plus VM e2e.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- Three distinct row icons ---------------------------


def test_row_renders_three_distinct_icons() -> None:
    """The row's action cell must surface three distinct icons."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # All three icons must be imported from lucide-react.
    for icon in ("Eye", "Pencil", "Trash2"):
        assert icon in src, (
            f"expected lucide '{icon}' icon to be imported and used on the row"
        )


def test_row_icons_have_distinct_click_handlers() -> None:
    """Each row icon must wire to a *distinct* handler — not all routed
    to the same setSelectedTask/sidebar opener like before."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # We expect dedicated handlers (or inline setters) for the three
    # actions. Accept any of these naming conventions.
    view_signals = ("openView", "setViewTask", "handleView", "onView")
    edit_signals = ("openEdit", "setEditTask", "handleEdit", "onEdit")
    delete_signals = ("openDelete", "setDeleteTask", "handleDelete", "onDelete")
    assert any(s in src for s in view_signals), (
        f"expected a view-handler signal in tasks-dashboard.tsx; "
        f"looked for any of {view_signals}"
    )
    assert any(s in src for s in edit_signals), (
        f"expected an edit-handler signal in tasks-dashboard.tsx; "
        f"looked for any of {edit_signals}"
    )
    assert any(s in src for s in delete_signals), (
        f"expected a delete-handler signal in tasks-dashboard.tsx; "
        f"looked for any of {delete_signals}"
    )


def test_action_cell_stops_propagation() -> None:
    """The icon buttons must not bubble back up to the row-level onClick
    (which still opens the legacy sidebar). Otherwise clicking the
    pencil would also open the sidebar — exactly the bug we're fixing."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "stopPropagation" in src, (
        "expected at least one stopPropagation call so the row-action "
        "icons don't bubble up to the row-level click handler"
    )


# ---------- View + Edit go to Dialog, NOT sidebar / Sheet ------


def test_view_and_edit_use_dialog_primitive() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # We reuse the existing shadcn Dialog (already in
    # components/ui/dialog.tsx).
    for name in (
        "Dialog",
        "DialogContent",
        "DialogHeader",
        "DialogFooter",
        "DialogTitle",
    ):
        assert name in src, f"expected Dialog primitive '{name}' to be imported"
    assert "@/components/ui/dialog" in src


def test_view_and_edit_do_not_use_sheet_or_sidebar_drawer() -> None:
    """Dennis explicitly wants the view + edit popups to be Dialog
    *modals*, not the sidebar Sheet — same as PR #36 (messages popup)
    and the in-flight agents UI fix."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # The Sheet primitive must not be wired into the row actions.
    assert "from '@/components/ui/sheet'" not in src, (
        "tasks-dashboard.tsx must not import Sheet — view/edit are Dialog modals"
    )


def test_view_dialog_renders_full_task_fields() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # The View modal labels every field — these are user-facing strings.
    for label in (
        "Task ID",
        "Title",
        "Description",
        "Status",
        "Priority",
        "Assigned",
        "Created",
        "Updated",
    ):
        assert label in src, (
            f"expected the view modal (or row context) to label '{label}'"
        )
    # multi-line description must be rendered with whitespace-pre-wrap.
    assert "whitespace-pre-wrap" in src, (
        "expected description rendered with whitespace-pre-wrap so "
        "newlines are preserved"
    )


# ---------- Edit dialog form fields ----------------------------


def test_edit_dialog_form_fields_present() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # The edit modal must include form controls / state for each
    # editable field. We accept either explicit state names or the
    # labels — both are very stable.
    for marker in (
        "editTitle",
        "editDescription",
        "editStatus",
        "editPriority",
        "editAssignedTo",
    ):
        assert marker in src, (
            f"expected edit-form state '{marker}' on the tasks page"
        )


def test_edit_dialog_save_calls_update_endpoint() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "apiClient.updateTask" in src, (
        "edit modal Save must call apiClient.updateTask (which targets "
        "POST /api/update-task-dashboard)"
    )


def test_edit_dialog_assigned_to_dropdown_uses_agents() -> None:
    """The Assigned To control must be a dropdown sourced from
    apiClient.getAgents() (so the admin can't typo an agent ID)."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "getAgents" in src, (
        "edit modal Assigned To dropdown must source options from "
        "apiClient.getAgents()"
    )


# ---------- Delete confirm + DELETE endpoint -------------------


def test_delete_confirm_dialog_present() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # Confirm copy matches the spec ("cannot be undone").
    assert "cannot be undone" in src or "Cannot be undone" in src, (
        "expected the delete confirm dialog to warn 'cannot be undone'"
    )


def test_delete_button_calls_delete_endpoint() -> None:
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "apiClient.deleteTask" in src, (
        "delete confirm must call apiClient.deleteTask (DELETE /api/tasks/<id>)"
    )


# ---------- updateTask signature includes mutable fields -------


def test_api_update_task_accepts_full_field_set() -> None:
    """updateTask used to only take {status, notes}; for the edit
    modal it must also accept title, description, priority,
    assigned_to so the admin can edit those fields."""
    src = _read("lib/api.ts")
    # Find the updateTask signature.
    m = re.search(r"async updateTask\([^)]*\)[^{]*\{.*?\n  \}", src, re.DOTALL)
    assert m, "updateTask not found in api.ts"
    body = m.group(0)
    for field in ("title", "description", "priority", "assigned_to"):
        assert field in body, (
            f"updateTask must support the '{field}' field in its data param"
        )
    # Must still POST to the upstream endpoint, not invent a new path.
    assert "update-task-dashboard" in body
