// Source audit — the two destructive confirms that live INSIDE a page
// component and therefore have no render seam of their own.
//
// `tasks-dashboard.tsx`'s <DeleteTaskDialog> and
// `schedules-dashboard.tsx`'s delete confirm are both un-exported
// internals of a page that would need the whole api-client + store
// graph mocked to render. Their siblings with a real seam
// (<DeleteConfirmModal>, <TerminateAgentDialog>, <RemoveProjectModal>)
// are pinned by RENDER tests in
// components/dashboard/destructive-confirm-a11y.test.tsx; these two get
// a source assertion instead, which is still enough to catch the
// regression that matters — someone editing the dialog and dropping the
// `alertDialog` opt-in.
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

describe("in-page destructive confirms opt in to role=alertdialog", () => {
  const cases: Array<[string, string, string]> = [
    [
      "tasks Delete-task dialog",
      "components/dashboard/tasks-dashboard.tsx",
      "<DialogTitle className=\"text-lg\">Delete task</DialogTitle>",
    ],
    [
      "schedules Delete-schedule dialog",
      "components/dashboard/schedules-dashboard.tsx",
      "<DialogTitle>Delete schedule</DialogTitle>",
    ],
  ]

  for (const [name, file, marker] of cases) {
    it(`${name} carries alertDialog`, () => {
      const tag = dialogContentTagFor(read(file), marker)
      expect(tag).toContain("alertDialog")
    })
  }
})

describe("destructive confirms keep Cancel before the destructive button", () => {
  // Radix autofocuses the first tabbable element in DOM order, so
  // footer ORDER is what keeps initial focus off the destructive
  // control (see destructive-confirm-a11y.test.tsx). `DialogFooter` is
  // `flex-col-reverse … sm:flex-row`, i.e. the visual right-most button
  // is the LAST in source — Cancel must come first.
  const cases: Array<[string, string, string]> = [
    [
      "tasks Delete-task dialog",
      "components/dashboard/tasks-dashboard.tsx",
      "<DialogTitle className=\"text-lg\">Delete task</DialogTitle>",
    ],
    [
      "schedules Delete-schedule dialog",
      "components/dashboard/schedules-dashboard.tsx",
      "<DialogTitle>Delete schedule</DialogTitle>",
    ],
  ]

  for (const [name, file, marker] of cases) {
    it(`${name}: Cancel precedes the destructive button`, () => {
      const src = read(file)
      const start = src.indexOf(marker)
      const footerStart = src.indexOf("<DialogFooter", start)
      const footerEnd = src.indexOf("</DialogFooter>", footerStart)
      expect(footerStart).toBeGreaterThan(-1)
      const footer = src.slice(footerStart, footerEnd)
      const cancelIdx = footer.indexOf("Cancel")
      const destructiveIdx = footer.indexOf('variant="destructive"')
      expect(cancelIdx).toBeGreaterThan(-1)
      expect(destructiveIdx).toBeGreaterThan(-1)
      expect(cancelIdx).toBeLessThan(destructiveIdx)
    })
  }
})
