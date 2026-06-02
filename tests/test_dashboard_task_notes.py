"""Regression guards for the Tasks-page notes feature in the popup dialogs.

Background. PR #49 (commit `9b55552`) replaced the legacy `TaskDetailsPanel`
sidebar with View / Edit / Delete dialogs. The legacy sidebar exposed an
"Add Note" action button (window.prompt-based) that POSTed the new note to
`/api/update-task-dashboard` via `apiClient.updateTask(taskId, { notes: str })`.
That panel was deleted in PR #49 but the Edit dialog the replacement built
DID NOT include any notes textarea, so as of `main` (post-PR #70) there is
no way to add a note from the dashboard UI. The View dialog still renders
notes but only when the list is non-empty — no empty state, no affordance
to add one when the list is empty.

This regression guard pins the restoration so it can't silently regress
again. Backend `/api/update-task-dashboard` already supports the `notes`
field (server appends `{timestamp, author, content}` to the JSON array);
this test only guards the *frontend wiring* that exposes it.

Notes are append-only — the backend has no per-note primary key. The brief
mentioned per-note edit / delete REST endpoints; those would require a
schema migration and are intentionally out of scope here. See
/tmp/task-notes-investigation-20260602-054631Z.md for the full rationale.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard/components/dashboard/tasks-dashboard.tsx")
API = Path("agent_mcp/dashboard/lib/api.ts")
ROUTES = Path("agent_mcp/app/routes.py")


def _src(p: Path) -> str:
    return p.read_text()


# ---------- View dialog ------------------------------------------------


def test_view_dialog_has_notes_section_label() -> None:
    """The View dialog must render a literal "Notes" section label so the
    admin can see the notes list (and the empty state when empty).
    Previously the section was gated on `notes.length > 0` so empty-notes
    tasks rendered no notes section at all — confusing because there's
    then no visible affordance saying "this task has no notes".
    """
    src = _src(DASHBOARD)
    # Match the Label/header for the notes section.
    assert re.search(r">Notes(\s*\(\{?notes\.length\}?\))?<", src), (
        "expected the View dialog to render a 'Notes' label/header. "
        "After PR #49 / #68, the notes block is gated on `notes.length > 0` "
        "and silently absent for empty-notes tasks."
    )


def test_view_dialog_notes_section_renders_unconditionally() -> None:
    """The Notes section must render even when the task has zero notes
    (with an empty-state message). Otherwise the admin can't tell whether
    the feature exists for empty tasks. The fix is to drop the
    `notes.length > 0 &&` guard on the section wrapper.
    """
    src = _src(DASHBOARD)
    # The legacy gating expression we want to be GONE.
    assert "{notes.length > 0 && (" not in src, (
        "expected the `{notes.length > 0 && (` guard around the View "
        "dialog Notes section to be removed — the section should always "
        "render with an empty state when there are no notes."
    )


def test_view_dialog_has_empty_notes_state() -> None:
    """When there are zero notes the View dialog should show an empty
    state ("No notes yet." or similar) rather than rendering nothing."""
    src = _src(DASHBOARD)
    # Accept either the exact phrase or a reasonable variant.
    assert re.search(r"No notes yet", src, re.IGNORECASE), (
        "expected an empty-state message like 'No notes yet.' in the "
        "View dialog Notes section so admins can see the section even "
        "when the task has no notes."
    )


# ---------- Edit dialog ------------------------------------------------


def test_edit_dialog_has_add_note_textarea() -> None:
    """The Edit dialog must have a `<Textarea>` field for adding a new
    note. Old design had an "Add Note" sidebar button using window.prompt;
    PR #49 replaced the sidebar but the Edit dialog never grew a notes
    textarea, so the feature was effectively lost.
    """
    src = _src(DASHBOARD)
    assert "edit-task-note" in src, (
        "expected an `id=\"edit-task-note\"` Textarea (or similar) in "
        "the Edit dialog for adding a new note. The Edit dialog "
        "currently has title / description / status / priority / "
        "assigned_to but no notes field, so the dashboard cannot append "
        "notes anywhere — feature regressed in PR #49."
    )


def test_edit_dialog_has_add_note_label() -> None:
    """The Add-note Textarea needs a visible label ("Add note" or "New
    note") so the admin knows what the field does. Distinguishing this
    from `description` matters because `notes` is *append* semantics
    (server adds a new entry to the JSON array) while `description` is
    *overwrite* semantics.
    """
    src = _src(DASHBOARD)
    assert re.search(r"(Add note|Add a note|New note)", src, re.IGNORECASE), (
        "expected a visible 'Add note' / 'New note' label in the Edit "
        "dialog near the notes textarea so the field's append semantics "
        "are clear (vs description which is overwrite)."
    )


def test_edit_dialog_submits_notes_in_patch() -> None:
    """The Edit dialog's save handler must include `notes:` in the patch
    body when the textarea has content, so the backend appends a new
    note entry. The patch object currently has title / description /
    status / priority / assigned_to only.
    """
    src = _src(DASHBOARD)
    # Look for a patch with `notes:` near the apiClient.updateTask call.
    # Allow either always-include + backend ignores empty, or conditional.
    update_call_region = re.search(
        r"const patch:[^}]*?\}",
        src,
        re.DOTALL,
    )
    assert update_call_region is not None, (
        "could not locate the `const patch:` literal in the Edit dialog "
        "save handler — test pattern needs updating."
    )
    region = update_call_region.group(0)
    assert "notes" in region or "editNote" in region, (
        "expected the Edit dialog save handler's patch object to include "
        "a `notes:` field (sourced from the new-note textarea) so "
        "submitting actually persists the note via the existing "
        "`/api/update-task-dashboard` endpoint."
    )


# ---------- ApiClient + types ------------------------------------------


def test_task_type_has_notes_field() -> None:
    """The Task TypeScript type must declare a `notes` array field with
    `{timestamp, author, content}` entries — that's the contract the
    server emits and the View dialog reads.
    """
    src = _src(API)
    # The notes field declaration in the Task interface.
    notes_field = re.search(
        r"notes\??:\s*(?:string\s*\|\s*)?Array<\s*\{[^}]*timestamp[^}]*author[^}]*content[^}]*\}",
        src,
        re.DOTALL,
    )
    assert notes_field is not None, (
        "expected Task.notes: Array<{timestamp, author, content}> in "
        "agent_mcp/dashboard/lib/api.ts so the View dialog has a typed "
        "shape to render."
    )


def test_api_client_update_task_accepts_notes_string() -> None:
    """`ApiClient.updateTask` must accept a `notes: string` field in the
    data param (server appends as a new note entry). This was added in
    PR #22; this guard pins it so a future refactor doesn't drop it.
    """
    src = _src(API)
    # Look at the updateTask signature data param.
    sig = re.search(
        r"async updateTask\([^)]*data:\s*\{[^}]*\}",
        src,
        re.DOTALL,
    )
    assert sig is not None, "could not locate updateTask signature"
    assert "notes?: string" in sig.group(0), (
        "expected `notes?: string` in updateTask's data param so the "
        "Edit dialog can append a new note via the existing "
        "`/api/update-task-dashboard` endpoint."
    )


# ---------- Backend route ----------------------------------------------


def test_update_task_dashboard_endpoint_accepts_notes() -> None:
    """The backend `/api/update-task-dashboard` route must still treat
    `notes` as a recognised editable key (it appends a new entry to the
    JSON array). The route is the persistence layer for the Edit dialog's
    Add-note textarea.
    """
    src = _src(ROUTES)
    # The EDITABLE_KEYS set in update_task_details_api_route.
    assert re.search(
        r"EDITABLE_KEYS\s*=\s*\{[^}]*\"notes\"[^}]*\}",
        src,
    ), (
        "expected `\"notes\"` in EDITABLE_KEYS inside "
        "update_task_details_api_route — without it the dashboard's "
        "Add-note submit returns a 400 'at least one editable field' "
        "error."
    )
    # The notes-append branch.
    assert "current_notes_list.append(new_note_entry)" in src, (
        "expected the notes-append branch in update_task_details_api_route "
        "that turns the `notes: str` body field into a new "
        "{timestamp, author, content} entry appended to the JSON array."
    )
