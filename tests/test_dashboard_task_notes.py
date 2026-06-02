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
    # Match the Label/header for the notes section. Accepts either the
    # legacy `Notes ({notes.length})` static label or the dynamic
    # variant that drops the count when there are zero notes.
    assert re.search(r">\s*Notes\b", src), (
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
    # Locate the EditTaskDialog component and look for both the patch
    # object and a downstream `notes` / `editNote` assignment before
    # `apiClient.updateTask`. The append can happen either inside the
    # initial object literal (`notes: ...`) or as a post-construction
    # `patch.notes = ...` (conditional on non-empty input).
    edit_dialog_region = re.search(
        r"const EditTaskDialog\b.*?await apiClient\.updateTask",
        src,
        re.DOTALL,
    )
    assert edit_dialog_region is not None, (
        "could not locate the EditTaskDialog's save handler — test "
        "pattern needs updating."
    )
    region = edit_dialog_region.group(0)
    assert re.search(r"\b(patch\.notes|notes:\s*\w+|editNote)\b", region), (
        "expected the Edit dialog save handler to wire the new-note "
        "textarea (`editNote` or similar) into the patch sent to "
        "`apiClient.updateTask`, either as `notes: trimmedNote` in the "
        "patch literal or as a `patch.notes = trimmedNote` assignment. "
        "Without it the textarea is decorative — the server never "
        "appends a new note."
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


# ---------- Integration: full dashboard Edit-payload round-trip --------
#
# bg-agent #61 wired the Add-note textarea into the patch sent by
# `EditTaskDialog.handleSave`. That fix is guarded statically above
# (`test_edit_dialog_submits_notes_in_patch`). Dennis reported the
# end-to-end flow still doesn't display notes after save, so we add a
# real integration test that simulates the EXACT payload the dashboard
# Edit dialog sends — full patch including title/description/status/
# priority/assigned_to AND `notes` — and asserts the notes appear in
# `GET /api/tasks` afterwards.
#
# This catches: dropped fields in sanitize_json_input, payload-shape
# mismatches (e.g. `notes` getting clobbered by an `assigned_to: null`
# branch), and the historical case where the route required `status`
# even for notes-only edits.


def test_dashboard_edit_payload_with_notes_round_trips(client) -> None:
    """Send the FULL Edit-dialog patch (title, description, status,
    priority, assigned_to, notes) and assert the note is persisted and
    shows up in `GET /api/tasks` with the expected
    {timestamp, author, content} shape.
    """
    import json as _json

    r = client.get("/api/tokens")
    assert r.status_code == 200, r.text
    token = r.json()["admin_token"]

    r = client.post(
        "/api/tasks",
        json={
            "token": token,
            "task_title": "round-trip note target",
            "task_description": "create + edit + verify",
        },
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    # The exact payload `EditTaskDialog.handleSave` builds when the
    # admin types a note and clicks Save.
    payload = {
        "token": token,
        "task_id": task_id,
        "title": "round-trip note target",
        "description": "create + edit + verify",
        "status": "pending",
        "priority": "medium",
        "assigned_to": None,
        "notes": "first dashboard note",
    }
    r = client.post("/api/update-task-dashboard", json=payload)
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True

    # Round-trip: fetch the tasks list and pull the note back out.
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    tasks = r.json()
    [task] = [t for t in tasks if t["task_id"] == task_id]
    notes_raw = task.get("notes")
    notes = _json.loads(notes_raw) if isinstance(notes_raw, str) else notes_raw
    assert isinstance(notes, list), f"notes must be a list, got {type(notes)}: {notes_raw!r}"
    assert len(notes) == 1, (
        f"expected exactly 1 note after one Save, got {len(notes)}: {notes}"
    )
    entry = notes[0]
    assert entry.get("content") == "first dashboard note", (
        f"note content did not round-trip; got {entry!r}"
    )
    assert "timestamp" in entry and "author" in entry, (
        f"note entry missing timestamp/author keys: {entry!r}"
    )


def test_dashboard_edit_payload_appends_multiple_notes(client) -> None:
    """Successive Saves with the notes textarea filled must APPEND
    entries to the array (not overwrite). The View dialog renders all
    historical notes; if a Save overwrites prior notes the audit trail
    is destroyed.
    """
    import json as _json

    token = client.get("/api/tokens").json()["admin_token"]
    task_id = client.post(
        "/api/tasks",
        json={"token": token, "task_title": "multi-note target"},
    ).json()["task_id"]

    base_payload = {
        "token": token,
        "task_id": task_id,
        "title": "multi-note target",
        "status": "pending",
        "priority": "medium",
        "assigned_to": None,
    }
    for content in ("note one", "note two", "note three"):
        r = client.post(
            "/api/update-task-dashboard",
            json={**base_payload, "notes": content},
        )
        assert r.status_code == 200, r.text

    tasks = client.get("/api/tasks").json()
    [task] = [t for t in tasks if t["task_id"] == task_id]
    notes_raw = task.get("notes")
    notes = _json.loads(notes_raw) if isinstance(notes_raw, str) else notes_raw
    contents = [n["content"] for n in notes]
    assert contents == ["note one", "note two", "note three"], (
        f"expected three notes appended in order, got {contents!r}"
    )


def test_dashboard_edit_payload_empty_notes_does_not_append(client) -> None:
    """When the Add-note textarea is empty, the dashboard omits `notes`
    from the patch (`apiClient.updateTask` skips `notes` when falsy).
    A Save that doesn't include `notes` must NOT append a phantom
    empty entry — otherwise every Save spams the notes log.
    """
    import json as _json

    token = client.get("/api/tokens").json()["admin_token"]
    task_id = client.post(
        "/api/tasks",
        json={"token": token, "task_title": "no-spam target"},
    ).json()["task_id"]

    # Save without the `notes` key — title-only edit.
    r = client.post(
        "/api/update-task-dashboard",
        json={
            "token": token,
            "task_id": task_id,
            "title": "no-spam target (edited)",
            "description": "",
            "status": "pending",
            "priority": "medium",
            "assigned_to": None,
        },
    )
    assert r.status_code == 200, r.text

    tasks = client.get("/api/tasks").json()
    [task] = [t for t in tasks if t["task_id"] == task_id]
    notes_raw = task.get("notes")
    notes = _json.loads(notes_raw) if isinstance(notes_raw, str) else (notes_raw or [])
    assert notes == [], (
        f"expected no notes appended for a notes-less Save, got {notes!r}"
    )
