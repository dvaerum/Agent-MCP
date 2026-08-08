// Source audit — destructive confirms that have no render seam of
// their own.
//
// HISTORY. This file was written against two dialogs that lived INSIDE
// a page component: `tasks-dashboard.tsx`'s <DeleteTaskDialog> and
// `schedules-dashboard.tsx`'s delete confirm. Both have since been
// migrated onto the shared tier-1 <ConfirmActionModal> — the tasks one
// by extraction into `tasks/delete-task-dialog.tsx` (it needed a test
// seam once its tier became conditional on the delete's blast radius),
// the schedules one in place.
//
// That migration is what makes the source audit BELOW different, not
// weaker. The guarantee (a destructive confirm carries the
// `alertDialog` opt-in, and its footer puts Cancel before the
// destructive button) is now enforced ONCE, on the shared modal, and
// covered by a real RENDER test in
// components/dashboard/modals/confirm-action-modal.test.tsx plus the
// a11y suite in components/dashboard/destructive-confirm-a11y.test.tsx.
// What remains here is the DELEGATION check: each page must actually
// route its confirm through a modal that carries the opt-in, so nobody
// can quietly hand-roll a bare <Dialog> back into a page.
//
// Two of the three call sites below are the same ones this file always
// audited; the third (memories) joins because its delete was
// downgraded from the type-to-confirm modal to the same tier-1 one.
import { describe, it, expect } from "vitest"
import { readFileSync } from "node:fs"
import path from "node:path"

const ROOT = path.resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(path.join(ROOT, rel), "utf8")
}

/**
 * The opening `<DialogContent …>` tag of the dialog whose body contains
 * `titleMarker`.
 */
function dialogContentTagFor(src: string, titleMarker: string): string {
  const titleIdx = src.indexOf(titleMarker)
  expect(titleIdx, `title marker ${titleMarker!} not found`).toBeGreaterThan(-1)
  const openIdx = src.lastIndexOf("<DialogContent", titleIdx)
  expect(openIdx).toBeGreaterThan(-1)
  const closeIdx = src.indexOf(">", openIdx)
  return src.slice(openIdx, closeIdx + 1)
}

// The shared tier-1 modal every in-page confirm now delegates to. Both
// guarantees this file used to assert per-page are asserted here once,
// at the place they are actually implemented.
const TIER1_MODAL = "components/dashboard/modals/confirm-action-modal.tsx"

describe("the shared tier-1 confirm opts in to role=alertdialog", () => {
  it("carries alertDialog on its DialogContent", () => {
    const tag = dialogContentTagFor(
      read(TIER1_MODAL),
      "<DialogTitle className=\"text-lg text-foreground\">{title}</DialogTitle>",
    )
    expect(tag).toContain("alertDialog")
  })

  it("keeps the mobile-width fallback on the same tag", () => {
    // A bare `<DialogContent>` with no className is invisible to the
    // pytest mobile audit (its regex requires one) AND clips on a
    // phone. The schedules confirm this modal replaced was exactly
    // that. Pin both properties on the one tag.
    const tag = dialogContentTagFor(
      read(TIER1_MODAL),
      "<DialogTitle className=\"text-lg text-foreground\">{title}</DialogTitle>",
    )
    expect(tag).toContain("w-[calc(100vw-2rem)]")
  })

  it("puts Cancel before the destructive button", () => {
    // Radix autofocuses the first tabbable element in DOM order, so
    // footer ORDER is what keeps initial focus off the destructive
    // control (see destructive-confirm-a11y.test.tsx). `DialogFooter`
    // is `flex-col-reverse … sm:flex-row`, i.e. the visual right-most
    // button is the LAST in source — Cancel must come first.
    const src = read(TIER1_MODAL)
    const footerStart = src.indexOf("<DialogFooter")
    const footerEnd = src.indexOf("</DialogFooter>", footerStart)
    expect(footerStart).toBeGreaterThan(-1)
    const footer = src.slice(footerStart, footerEnd)
    const cancelIdx = footer.indexOf("Cancel")
    const destructiveIdx = footer.indexOf('variant="destructive"')
    expect(cancelIdx).toBeGreaterThan(-1)
    expect(destructiveIdx).toBeGreaterThan(-1)
    expect(cancelIdx).toBeLessThan(destructiveIdx)
  })
})

describe("in-page destructive confirms delegate to a modal that has it", () => {
  const cases: Array<[string, string]> = [
    ["tasks Delete-task dialog", "components/dashboard/tasks/delete-task-dialog.tsx"],
    ["schedules Delete-schedule dialog", "components/dashboard/schedules-dashboard.tsx"],
    ["memories Delete-memory dialog", "components/dashboard/memories-dashboard.tsx"],
  ]

  for (const [name, file] of cases) {
    it(`${name} routes through <ConfirmActionModal>`, () => {
      const src = read(file)
      expect(src).toContain("<ConfirmActionModal")
      // …and does NOT hand-roll a bare <Dialog> confirm alongside it.
      // (Pages legitimately carry OTHER dialogs — create/edit forms —
      // so this only asserts the confirm itself was migrated, via the
      // absence of the old delete-confirm titles in raw Dialog markup.)
      expect(src).not.toMatch(
        /<DialogTitle[^>]*>\s*Delete (task|schedule|memory)\s*<\/DialogTitle>/,
      )
    })
  }
})
