/**
 * Regression guards for the dashboard Tasks page row-action icons.
 *
 * Before this PR every row icon on the Tasks page eventually opened the
 * same sidebar (TaskDetailsPanel) — the three buttons did the same
 * thing. This PR splits the row into three distinct icon actions, each
 * backed by a Dialog modal (NOT the sidebar):
 *
 * - Eye    -> read-only "View" Dialog showing every task field.
 * - Pencil -> "Edit" Dialog letting an admin mutate the task fields and
 *             saving via POST /api/update-task-dashboard (+ assigned_to
 *             via the same endpoint, extended in this PR).
 * - Trash2 -> Delete confirm Dialog, then DELETE /api/tasks/<id>
 *             (PR #12) with the admin token.
 *
 * Text-parse regression guards (same convention as
 * test_dashboard_messages_detail_popup.py and
 * test_dashboard_agent_restore_purge.py, now ported to this vitest
 * convention); we don't have jsdom in this repo and behaviour is
 * verified by `npm run build` plus VM e2e.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, existsSync } from "node:fs"
import { resolve } from "node:path"

// Resolve relative to this test file so the test runs identically from
// the dashboard dir, repo root, or CI cwd.
const DASHBOARD_ROOT = resolve(__dirname, "..")

// Wave 5 (refactor/w5-tasks): the Tasks page was split into a page
// module + a `tasks/` satellite directory (the row column spec, the
// View / Edit dialogs). These guards assert properties of the PAGE, so
// read the page + its satellites as one blob — same list + order as
// the Python `tests/dashboard_sources.py::TASKS_SOURCES` helper this
// test was ported from. Keep this list in sync when a Tasks satellite
// is added or removed, or the guards below silently narrow their audit
// surface.
const TASKS_SOURCES = [
  "components/dashboard/tasks-dashboard.tsx",
  "components/dashboard/tasks/tasks-api.ts",
  "components/dashboard/tasks/use-tasks-columns.tsx",
  "components/dashboard/tasks/create-task-modal.tsx",
  "components/dashboard/tasks/view-task-dialog.tsx",
  "components/dashboard/tasks/edit-task-dialog.tsx",
  "components/dashboard/tasks/delete-task-dialog.tsx",
  "components/dashboard/tasks/tasks-pagination.tsx",
  "components/dashboard/tasks-mobile-list.tsx",
]

function tasksPageSource(): string {
  const missing = TASKS_SOURCES.filter(
    (rel) => !existsSync(resolve(DASHBOARD_ROOT, rel)),
  )
  expect(
    missing,
    `Tasks page satellite(s) missing — the source-grep guards would ` +
      `silently stop auditing them: ${missing.join(", ")}`,
  ).toEqual([])
  return TASKS_SOURCES.map((rel) =>
    readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8"),
  ).join("\n")
}

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

const DELETE_DIALOG = "components/dashboard/tasks/delete-task-dialog.tsx"

// ---------- Three distinct row icons ---------------------------

describe("Tasks page row icons", () => {
  it("renders three distinct icons on the row's action cell", () => {
    const src = tasksPageSource()
    // All three icons must be imported from lucide-react.
    for (const icon of ["Eye", "Pencil", "Trash2"]) {
      expect(
        src.includes(icon),
        `expected lucide '${icon}' icon to be imported and used on the row`,
      ).toBe(true)
    }
  })

  it("wires each row icon to a distinct click handler — not all routed to the same setSelectedTask/sidebar opener like before", () => {
    const src = tasksPageSource()
    // We expect dedicated handlers (or inline setters) for the three
    // actions. Accept any of these naming conventions.
    const viewSignals = ["openView", "setViewTask", "handleView", "onView"]
    const editSignals = ["openEdit", "setEditTask", "handleEdit", "onEdit"]
    const deleteSignals = [
      "openDelete",
      "setDeleteTask",
      "handleDelete",
      "onDelete",
    ]
    expect(
      viewSignals.some((s) => src.includes(s)),
      `expected a view-handler signal in tasks-dashboard.tsx; looked for any of ${viewSignals}`,
    ).toBe(true)
    expect(
      editSignals.some((s) => src.includes(s)),
      `expected an edit-handler signal in tasks-dashboard.tsx; looked for any of ${editSignals}`,
    ).toBe(true)
    expect(
      deleteSignals.some((s) => src.includes(s)),
      `expected a delete-handler signal in tasks-dashboard.tsx; looked for any of ${deleteSignals}`,
    ).toBe(true)
  })

  it("stops propagation in the action cell so the icon buttons don't bubble back up to the row-level onClick (which still opens the legacy sidebar) — otherwise clicking the pencil would also open the sidebar, exactly the bug we're fixing", () => {
    const src = tasksPageSource()
    expect(
      src.includes("stopPropagation"),
      "expected at least one stopPropagation call so the row-action icons don't bubble up to the row-level click handler",
    ).toBe(true)
  })

  // ---------- View + Edit go to Dialog, NOT sidebar / Sheet ------

  it("uses the Dialog primitive for View + Edit", () => {
    const src = tasksPageSource()
    // We reuse the existing shadcn Dialog (already in
    // components/ui/dialog.tsx).
    for (const name of [
      "Dialog",
      "DialogContent",
      "DialogHeader",
      "DialogFooter",
      "DialogTitle",
    ]) {
      expect(
        src.includes(name),
        `expected Dialog primitive '${name}' to be imported`,
      ).toBe(true)
    }
    expect(src.includes("@/components/ui/dialog")).toBe(true)
  })

  it("does not use Sheet or the sidebar drawer for View + Edit — Dennis explicitly wants the view + edit popups to be Dialog modals, not the sidebar Sheet, same as PR #36 (messages popup) and the in-flight agents UI fix", () => {
    const src = tasksPageSource()
    // The Sheet primitive must not be wired into the row actions.
    expect(
      src.includes("from '@/components/ui/sheet'"),
      "tasks-dashboard.tsx must not import Sheet — view/edit are Dialog modals",
    ).toBe(false)
  })

  it("renders the full task fields in the view dialog", () => {
    const src = tasksPageSource()
    // The View modal labels every field — these are user-facing strings.
    for (const label of [
      "Task ID",
      "Title",
      "Description",
      "Status",
      "Priority",
      "Assigned",
      "Created",
      "Updated",
    ]) {
      expect(
        src.includes(label),
        `expected the view modal (or row context) to label '${label}'`,
      ).toBe(true)
    }
    // multi-line description must be rendered with whitespace-pre-wrap.
    expect(
      src.includes("whitespace-pre-wrap"),
      "expected description rendered with whitespace-pre-wrap so newlines are preserved",
    ).toBe(true)
  })

  // ---------- Edit dialog form fields ----------------------------

  it("has the edit dialog form fields present", () => {
    const src = tasksPageSource()
    // The edit modal must include form controls / state for each
    // editable field. We accept either explicit state names or the
    // labels — both are very stable.
    for (const marker of [
      "editTitle",
      "editDescription",
      "editStatus",
      "editPriority",
      "editAssignedTo",
    ]) {
      expect(
        src.includes(marker),
        `expected edit-form state '${marker}' on the tasks page`,
      ).toBe(true)
    }
  })

  it("calls the update endpoint on edit dialog save", () => {
    const src = tasksPageSource()
    expect(
      src.includes("apiClient.updateTask"),
      "edit modal Save must call apiClient.updateTask (which targets POST /api/update-task-dashboard)",
    ).toBe(true)
  })

  it("sources the Assigned To dropdown from apiClient.getAgents() so the admin can't typo an agent ID", () => {
    const src = tasksPageSource()
    expect(
      src.includes("getAgents"),
      "edit modal Assigned To dropdown must source options from apiClient.getAgents()",
    ).toBe(true)
  })

  // ---------- Delete confirm + DELETE endpoint -------------------
  //
  // The delete confirm was extracted to
  // `components/dashboard/tasks/delete-task-dialog.tsx` when its
  // confirmation tier became conditional on the delete's blast radius
  // (leaf = one-click, sub-tree = type DELETE). The two guarantees
  // below follow the markup to its new home; the "wired into the
  // page" guarantee keeps the page on the hook for actually rendering
  // it.

  it("has the delete confirm dialog warn that the action cannot be undone", () => {
    const src = read(DELETE_DIALOG)
    // Confirm copy matches the spec ("cannot be undone").
    expect(
      src.includes("cannot be undone") || src.includes("Cannot be undone"),
      "expected the delete confirm dialog to warn 'cannot be undone'",
    ).toBe(true)
  })

  it("wires the delete dialog into the page via the Trash2 row action", () => {
    const src = tasksPageSource()
    expect(
      src.includes("<DeleteTaskDialog"),
      "the Trash2 row action must still open <DeleteTaskDialog>",
    ).toBe(true)
  })

  it("calls the delete endpoint on delete button confirm", () => {
    const src = read(DELETE_DIALOG)
    expect(
      src.includes("apiClient.deleteTask"),
      "delete confirm must call apiClient.deleteTask (DELETE /api/tasks/<id>)",
    ).toBe(true)
  })

  it("requires explicit force for the cascade delete — the dashboard may only ask for the destructive cascade from the branch that showed the blast radius AND made the operator type DELETE; force: true anywhere else would re-disarm the backend guard this dialog exists to keep armed", () => {
    const src = read(DELETE_DIALOG)
    expect(
      src.includes("runDelete(true)") && src.includes("runDelete(false)"),
      "delete dialog must have both a forced and an unforced path",
    ).toBe(true)
    expect(
      (src.match(/runDelete\(true\)/g) ?? []).length,
      "only ONE call site may request the cascade",
    ).toBe(1)
    // ...and it must be the DeleteConfirmModal (type-to-confirm) branch.
    const forced = src.indexOf("runDelete(true)")
    expect(
      src.slice(0, forced).includes("DeleteConfirmModal"),
      "the forced delete must sit inside the type-to-confirm branch",
    ).toBe(true)
    expect(
      src.slice(forced).includes("ConfirmActionModal"),
      "the unforced (tier-1) branch must come after the forced one",
    ).toBe(true)
  })

  // ---------- updateTask signature includes mutable fields -------

  it("has updateTask accept the full mutable field set — it used to only take {status, notes}; for the edit modal it must also accept title, description, priority, assigned_to so the admin can edit those fields", () => {
    const src = read("lib/api/tasks.ts")
    // Find the updateTask method. W6-followup F1 moved it into the
    // tasks resource bundle (object-literal method, ~4-space indent, no
    // `async` keyword since it just returns core.request), so match the
    // bare `updateTask(` and close on the method's own brace.
    const m = src.match(/updateTask\([^)]*\)[^{]*\{[\s\S]*?\n\s{4}\}/)
    expect(m, "updateTask not found in lib/api/tasks.ts").not.toBeNull()
    const body = m![0]!
    for (const field of ["title", "description", "priority", "assigned_to"]) {
      expect(
        body.includes(field),
        `updateTask must support the '${field}' field in its data param`,
      ).toBe(true)
    }
    // Must still POST to the upstream endpoint, not invent a new path.
    expect(body.includes("update-task-dashboard")).toBe(true)
  })
})
