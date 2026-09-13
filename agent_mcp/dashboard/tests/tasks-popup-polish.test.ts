/**
 * Regression guards for Phase 7-UX1: Tasks page row-click + popup polish.
 *
 * This PR removes the legacy `TaskDetailsPanel` sidebar from the Tasks
 * dashboard. Clicking a task row body now opens the same View dialog as
 * the eye icon. The View / Edit / Delete dialogs are also re-laid out per
 * shadcn idiom (sized containers, labels above values, footer button
 * order Cancel-left / primary-right, destructive variant on delete).
 *
 * These tests are text-parse regression guards (same convention as the
 * rest of this suite). No jsdom in this repo; behaviour is verified by
 * `npm run build` plus Firefox MCP e2e.
 *
 * Ported from tests/test_dashboard_tasks_popup_polish.py (Python source
 * tree retired).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { tasksPageSource } from "./support/tasks-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

// Wave 5 (refactor/w5-tasks): the Tasks page was split into a page
// module + a `tasks/` satellite directory (the View / Edit dialogs, the
// column spec). These guards assert page-level properties, so read the
// page + its satellites as one blob. See tests/support/tasks-source.ts.
function tasksSrc(): string {
  return tasksPageSource()
}

// Wave 5 satellites the dialog layout guards follow.
const VIEW_TASK_DIALOG = "components/dashboard/tasks/view-task-dialog.tsx"
const EDIT_TASK_DIALOG = "components/dashboard/tasks/edit-task-dialog.tsx"
// The create/edit dialogs adopted the shared <FormDialog> shell, which
// now OWNS the DialogContent width + the Cancel/Save footer — so the
// guarantees about those (mobile-safe width, Cancel-before-primary) are
// audited at the shell, exactly as the delete guarantees delegate to
// <ConfirmActionModal>. Not a weakening: every original assertion is
// still made, just at the component where the markup now lives.
const FORM_DIALOG = "components/dashboard/shared/form-dialog.tsx"
// The Delete dialog was EXTRACTED out of the page god-file (it needed a
// test seam once its confirmation tier became conditional on the blast
// radius). The three delete guarantees below therefore audit the
// component where the markup now lives — plus the page, which must still
// render it. Following a file that moved is not the same as weakening
// what is asserted: every original assertion is still made, and
// "delete dialog is rendered by the page" is a NEW assertion that stops
// the redirection from becoming an escape hatch.
const DELETE_DIALOG = "components/dashboard/tasks/delete-task-dialog.tsx"
const CONFIRM_ACTION_MODAL = "components/dashboard/modals/confirm-action-modal.tsx"

// ---------- Legacy sidebar gone -----------------------------------

describe("legacy TaskDetailsPanel sidebar removed", () => {
  it("no longer imports TaskDetailsPanel", () => {
    const src = tasksSrc()
    expect(
      src.includes('from "./task-details-panel"'),
      "TaskDetailsPanel import must be removed from tasks-dashboard.tsx",
    ).toBe(false)
    expect(
      src.includes("from './task-details-panel'"),
      "TaskDetailsPanel import must be removed from tasks-dashboard.tsx",
    ).toBe(false)
  })

  it("no longer renders <TaskDetailsPanel />", () => {
    const src = tasksSrc()
    expect(
      src.includes("<TaskDetailsPanel"),
      "TaskDetailsPanel must not be rendered in tasks-dashboard.tsx; " +
        "the sidebar has been retired in favour of the View dialog",
    ).toBe(false)
  })
})

// ---------- Row body click opens the View dialog -------------------

describe("row body click opens the View dialog", () => {
  it("routes to openView(task.task_id) via TableRow onClick or DataTablePage onRowClick", () => {
    // The row-body `onClick` must route to the same `openView` handler
    // used by the eye icon — clicking anywhere on the row body opens
    // the View dialog now.
    //
    // After the 2026-06-02 live-lookup refactor (Candidate D), the
    // handler takes the row's identity field (`task.task_id`) rather
    // than the row itself so the dialog can read the row live from the
    // source.
    //
    // Two accepted shapes, because the row markup moved into the shared
    // scaffold (PR #581): the pre-scaffold `<TableRow onClick={() =>
    // openView(task.task_id)}>`, or the scaffold's row-click slot
    // `<DataTablePage onRowClick={(task) => openView(task.task_id)}>`.
    // The GUARANTEE is unchanged — a row-body click routes to the same
    // `openView` the eye icon uses — only the prop that carries it
    // differs. Neither shape accepts the legacy handleTaskClick /
    // setSelectedTask sidebar path.
    const src = tasksSrc()
    const tableRowClick = /onClick=\{\(\)\s*=>\s*openView\(task\.task_id\)\}/.test(src)
    const scaffoldRowClick = /onRowClick=\{\(task\)\s*=>\s*openView\(task\.task_id\)\}/.test(
      src,
    )
    expect(
      tableRowClick || scaffoldRowClick,
      "the row body must call openView(task.task_id) — either as the " +
        "TableRow `onClick={() => openView(task.task_id)}` or as the " +
        "<DataTablePage> `onRowClick={(task) => openView(task.task_id)}` " +
        "— so the row body opens the View dialog (same as the eye icon) " +
        "and the live-lookup useDialog reads the row from the store on " +
        "every render",
    ).toBe(true)
  })

  it("removes the legacy selectedTask sidebar state", () => {
    // The `selectedTask` state (used to drive the sidebar) must be
    // gone; otherwise the sidebar is half-wired and could resurrect.
    const src = tasksSrc()
    expect(
      src.includes("selectedTask"),
      "selectedTask state must be removed; the sidebar is retired",
    ).toBe(false)
    expect(
      src.includes("setSelectedTask"),
      "setSelectedTask must be removed; row-body click now opens " +
        "the View dialog directly",
    ).toBe(false)
  })
})

// ---------- Dialog widths follow shadcn idiom ---------------------

describe("dialog widths follow shadcn idiom", () => {
  it("View dialog has an explicit width override (sm:!max-w-3xl)", () => {
    const src = tasksSrc()
    // The ViewTaskDialog's DialogContent must declare a width override
    // that beats the base DialogContent's `sm:max-w-lg` (which
    // otherwise wins the cascade because both share specificity and
    // base is declared later in the merged className string).
    //
    // Originally `max-w-2xl`; updated to `sm:!max-w-3xl` (Tailwind
    // important) after the Firefox MCP audit found base `sm:max-w-lg`
    // was squeezing every desktop dialog to 512px.
    expect(
      /ViewTaskDialog[\s\S]*?DialogContent[^>]*sm:!max-w-3xl/.test(src),
      "View dialog DialogContent must use sm:!max-w-3xl to override base sm:max-w-lg",
    ).toBe(true)
  })

  it("Edit dialog renders <FormDialog wide>, which owns sm:max-w-2xl", () => {
    // Wave 5: the Edit dialog adopted the shared <FormDialog> shell,
    // which OWNS the DialogContent width. Satisfied by delegation (like
    // the delete tests delegate to <ConfirmActionModal>): the edit
    // dialog renders <FormDialog wide>, and the wide width lives on
    // FormDialog.
    const editSrc = read(EDIT_TASK_DIALOG)
    expect(
      /<FormDialog\b/.test(editSrc) && editSrc.includes("wide"),
      "Edit dialog must render the shared <FormDialog wide> shell " +
        "(which owns the desktop-comfortable dialog width)",
    ).toBe(true)
    const formSrc = read(FORM_DIALOG)
    expect(
      formSrc.includes("sm:max-w-2xl"),
      "FormDialog's `wide` width (sm:max-w-2xl) must beat the base " +
        "sm:max-w-lg so the Edit dialog isn't squeezed on desktop",
    ).toBe(true)
  })

  it("Delete dialog delegates max-w-md sizing to ConfirmActionModal", () => {
    // Satisfied by delegation: <DeleteTaskDialog> renders the shared
    // <ConfirmActionModal> (tier 1) / <DeleteConfirmModal> (tier 2), and
    // the sizing now lives on those. Assert it there.
    const src = read(CONFIRM_ACTION_MODAL)
    expect(
      /<DialogContent[^>]*max-w-md/s.test(src),
      "Delete dialog DialogContent must use max-w-md",
    ).toBe(true)
  })

  it("the page actually renders <DeleteTaskDialog>", () => {
    // Guard the redirection above: the Tasks page must actually render
    // the extracted dialog, or the three delete guarantees would be
    // auditing a component nobody uses.
    const src = tasksSrc()
    expect(
      src.includes("<DeleteTaskDialog"),
      "tasks-dashboard.tsx must render <DeleteTaskDialog>",
    ).toBe(true)
    expect(
      src.includes("tasks/delete-task-dialog"),
      "tasks-dashboard.tsx must import the extracted delete dialog",
    ).toBe(true)
  })
})

// ---------- Scrollable body, not the whole modal ------------------

describe("View dialog scroll region", () => {
  it("caps at 90dvh with a single flex-1 min-h-0 overflow-y-auto scroll body", () => {
    // Dialog body section scrolls (not the whole modal): the dialog caps
    // at the viewport and a `flex-1 min-h-0 overflow-y-auto` body is the
    // single scroll region.
    //
    // Wave 5: the Edit dialog moved to the shared <FormDialog> (which
    // owns an equivalent viewport cap + scroll body). The View dialog
    // keeps its own layout, so the guarantee is pinned there:
    // `max-h-[90dvh]` on the DialogContent + a `flex-1 min-h-0
    // overflow-y-auto` scroll body. `dvh`, not `vh` — see
    // docs/learnings/dashboard-dialog-mobile-clipping.md.
    const src = tasksSrc()
    expect(
      src.includes("max-h-[90dvh]"),
      "View dialog must cap height at 90dvh so long tasks scroll inside " +
        "the modal instead of stretching the page",
    ).toBe(true)
    expect(
      src.includes("flex-1 min-h-0 overflow-y-auto"),
      "the dialog body must be the single `flex-1 min-h-0 " +
        "overflow-y-auto` scroll region",
    ).toBe(true)
  })
})

// ---------- Label primitive used for labels above values ----------

describe("dialogs use the Label primitive", () => {
  it("imports and renders shadcn <Label>", () => {
    // Labels above inputs should use the shadcn `Label` primitive, not
    // raw `<label>` tags — matches the Agents page polish.
    const src = tasksSrc()
    expect(
      src.includes("@/components/ui/label"),
      "shadcn Label must be imported and used for input labels",
    ).toBe(true)
    // Must actually render <Label> in the dialogs.
    expect(
      /<Label\b/.test(src),
      "shadcn <Label> primitive must be rendered in the dialogs",
    ).toBe(true)
  })
})

// ---------- Footer button order: Cancel on left, primary on right --

function footerBlock(src: string, dialogNamePattern: string): string {
  // Locate the dialog's component block and return ONLY the JSX
  // between <DialogFooter ...> and </DialogFooter> — not the whole
  // component (which would include unrelated mentions like the
  // "Saving…" button label or the dialog title text).
  const m = new RegExp(
    `${dialogNamePattern}[\\s\\S]*?(<DialogFooter[\\s\\S]*?</DialogFooter>)`,
  ).exec(src)
  expect(m, `No DialogFooter found inside ${dialogNamePattern}`).not.toBeNull()
  return m![1]!
}

describe("footer button order: Cancel left, primary/destructive right", () => {
  it("Edit dialog's FormDialog footer places Cancel before the primary submit", () => {
    // Wave 5: the Edit dialog's footer is now owned by the shared
    // <FormDialog> shell (Cancel + submit), so the Cancel-left /
    // primary-right guarantee is pinned there — the same delegation the
    // delete-dialog footer test uses for <ConfirmActionModal>. First
    // confirm the Edit dialog actually renders the shell.
    const editSrc = read(EDIT_TASK_DIALOG)
    expect(
      /<FormDialog\b/.test(editSrc),
      "Edit dialog must render the shared <FormDialog> shell",
    ).toBe(true)
    const src = read(FORM_DIALOG)
    const m = /<DialogFooter[\s\S]*?<\/DialogFooter>/.exec(src)
    expect(m, "No DialogFooter found in FormDialog").not.toBeNull()
    const footer = m![0]
    // Cancel button closes the dialog; submit runs the mutation.
    const cancelIdx = footer.indexOf("onOpenChange(false)")
    const submitIdx = footer.indexOf("void submit()")
    expect(cancelIdx !== -1, "FormDialog footer missing Cancel button").toBe(true)
    expect(submitIdx !== -1, "FormDialog footer missing submit button").toBe(true)
    expect(
      cancelIdx < submitIdx,
      "FormDialog footer must place Cancel before the primary submit " +
        "(Cancel-left, primary-right)",
    ).toBe(true)
  })

  it("Delete dialog's ConfirmActionModal footer places Cancel before the destructive confirm", () => {
    // Delegated to the shared tier-1 modal (see the module note above).
    const src = read(CONFIRM_ACTION_MODAL)
    const footer = footerBlock(src, "ConfirmActionModal\\(")
    const cancelIdx = footer.indexOf("Cancel")
    // The confirm label is a prop (it reads "Delete" / "Delete N tasks" /
    // "Terminate" per call site), so the destructive button is located by
    // its variant rather than by literal text — a tighter anchor than the
    // word "Delete", not a looser one.
    const deleteIdx = footer.indexOf('variant="destructive"')
    expect(cancelIdx !== -1, "Delete dialog footer missing Cancel button").toBe(true)
    expect(deleteIdx !== -1, "Delete dialog footer missing destructive button").toBe(true)
    expect(
      cancelIdx < deleteIdx,
      "Delete dialog footer must place Cancel before the destructive " +
        "Delete confirm (Cancel-left, destructive-right)",
    ).toBe(true)
  })

  it("Delete dialog's confirm button uses the destructive variant", () => {
    // Delegated to the shared tier-1 modal (see the module note above).
    const src = read(CONFIRM_ACTION_MODAL)
    expect(
      /ConfirmActionModal\([\s\S]*?variant="destructive"/.test(src),
      'Delete dialog confirm button must use variant="destructive"',
    ).toBe(true)
  })
})

// ---------- Selects + inputs full-width inside their cell ---------

describe("Edit dialog inputs are full-width", () => {
  it("SelectTrigger/Input declare w-full", () => {
    // Wave 5: EditTaskDialog is its own satellite now (adopted
    // <FormDialog>, so no more `React.memo(...) … displayName` wrapper to
    // anchor on). Read the satellite directly. SelectTrigger/Input
    // inside must declare w-full so the controls fill their column.
    const body = read(EDIT_TASK_DIALOG)
    expect(
      body.includes("w-full"),
      "Edit dialog SelectTrigger/Input must use w-full so controls " +
        "fill their column",
    ).toBe(true)
  })
})

// ---------- Monospace task_id footer in View dialog ---------------

describe("View dialog task_id uses monospace", () => {
  it("renders task.task_id in font-mono", () => {
    // Wave 5: ViewTaskDialog is its own satellite now. Read it directly.
    const body = read(VIEW_TASK_DIALOG)
    // task_id must be rendered with font-mono.
    expect(
      body.includes("font-mono"),
      "View dialog must render task_id (and other code-like fields) " +
        "in font-mono",
    ).toBe(true)
    expect(body.includes("task.task_id"), "View dialog must surface task.task_id").toBe(
      true,
    )
  })
})
