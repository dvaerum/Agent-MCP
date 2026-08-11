/**
 * AX-5 — no native window.alert()/confirm() in the components tree.
 *
 * Native alert()/confirm() are unstyled, block the event loop, and are
 * not routed through the app's accessible toast/dialog surfaces. The
 * class was swept: server-picker confirms moved to <ConfirmActionModal>
 * (use-server-confirms.tsx), and manual-server-input's port-validation
 * alert() became a shared toast.
 *
 * Grep-based on source bytes (matching the project's Vitest baseline).
 * Comments legitimately mention the words alert/confirm; we assert on
 * CALL syntax (`alert(` / `confirm(`) after stripping comments so the
 * documentation of the migration doesn't trip the guard.
 */
import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { resolve } from "node:path"

const COMPONENTS = resolve(__dirname, "..", "components")

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = resolve(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p)
  }
  return out
}

/** Strip // line comments and block comments so prose can't match. */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1")
}

describe("AX-5: no native alert()/confirm() in components", () => {
  const files = walk(COMPONENTS)

  it("finds source files to scan", () => {
    expect(files.length).toBeGreaterThan(0)
  })

  for (const file of files) {
    it(`${file.replace(COMPONENTS, "components")} has no native alert()/confirm() call`, () => {
      const code = stripComments(readFileSync(file, "utf8"))
      // Bare or window-qualified call, but NOT a member like
      // `.confirm(` (none exist today) and NOT `confirmLabel`/AlertDialog.
      expect(/(?<![.\w])(?:window\.)?alert\s*\(/.test(code)).toBe(false)
      expect(/(?<![.\w])(?:window\.)?confirm\s*\(/.test(code)).toBe(false)
    })
  }
})
