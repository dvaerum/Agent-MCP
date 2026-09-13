/**
 * Regression-guard pin: every View/Detail dialog's footer wraps
 * buttons instead of stacking them full-width on mobile.
 *
 * Background. Four dialogs across the dashboard render a read-only
 * "view" of an entity with Edit/Delete/secondary actions plus Close:
 * `view-task-dialog.tsx`, `agent-detail-dialog.tsx` (up to 5 buttons —
 * Send directive / Edit / Terminate / Purge / Close), `view-message-
 * modal.tsx`, and `view-memory-modal.tsx`. Three of the four used the
 * shared `<DialogFooter>` directly, whose default
 * (`flex-col-reverse` below `sm:`) stacks every button full-width on a
 * phone — reported live: a 3-button task-detail popup ate roughly a
 * third of a 390x844 viewport on buttons alone. The fourth
 * (`view-memory-modal.tsx`) hand-rolled its own always-row footer
 * instead, so it never regressed the same way but also never shared the
 * fix.
 *
 * `shared/view-dialog-footer.tsx` promotes a `<ViewDialogFooter>` that
 * wraps `<DialogFooter>` with a row+wrap layout, LEAVING `<DialogFooter>`
 * itself untouched — its stacked-by-default mobile behavior is
 * deliberate for confirm dialogs (Cancel-vs-Delete touch-target safety;
 * see `confirm-action-modal.tsx`'s own doc comment) and this fix must not
 * change that.
 *
 * Scope is derived by filename convention (`view-*.tsx` /
 * `*-detail-dialog.tsx`) rather than a hardcoded list — matches every
 * known offender today and picks up a future dialog following the same
 * naming without an edit here.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const DASHBOARDS = resolve(DASHBOARD_ROOT, "components", "dashboard")
const SHARED_FOOTER = resolve(DASHBOARDS, "shared", "view-dialog-footer.tsx")

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = resolve(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

function viewDialogFiles(): string[] {
  const all = walk(DASHBOARDS)
  const found = all.filter((f) => {
    const base = f.split("/").pop()!
    const matchesPattern =
      (base.startsWith("view-") && base.endsWith(".tsx")) ||
      base.endsWith("-detail-dialog.tsx")
    if (!matchesPattern) return false
    if (base.endsWith(".test.tsx")) return false
    if (f === SHARED_FOOTER) return false
    return true
  })
  return [...new Set(found)].sort()
}

describe("View/Detail dialog footer", () => {
  it("uses the shared wrapping footer — every View/Detail dialog must render its action-button footer through <ViewDialogFooter>, not a raw <DialogFooter> (which stacks buttons full-width on mobile) or a hand-rolled footer div (which never gets the fix at all)", () => {
    const files = viewDialogFiles()
    expect(
      files.length > 0,
      "view-dialog audit set derived empty — the derivation is broken",
    ).toBe(true)

    const failures: string[] = []
    for (const f of files) {
      const src = readFileSync(f, "utf8")
      if (!src.includes("<ViewDialogFooter")) {
        failures.push(`${f}: no <ViewDialogFooter> usage found`)
      }
    }
    expect(
      failures,
      "View/Detail dialog(s) not on the shared wrapping footer — " +
        "buttons will stack full-width on mobile instead of wrapping " +
        `next to each other:\n  ${failures.join("\n  ")}`,
    ).toEqual([])
  })
})
