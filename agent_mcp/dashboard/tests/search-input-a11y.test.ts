/**
 * AX-3 — Accessible name for every list-dashboard search box.
 *
 * A `placeholder` is NOT an accessible name: many screen readers do
 * not announce it, and it vanishes the moment the user types. Each
 * "Search …" textbox must therefore carry an explicit `aria-label`.
 *
 * Grep-based on source bytes, matching the project's Vitest baseline
 * (see tests/user-form-hardening.test.ts) — the property we pin is a
 * shape of the source, not a runtime behaviour, and rendering the full
 * dashboards would need the entire store graph mocked to assert a
 * single static attribute.
 */
import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// Each entry: the dashboard source, the search placeholder it renders,
// and the accessible name the search Input must expose.
const CASES: Array<{ file: string; placeholder: RegExp; label: string }> = [
  {
    file: "components/dashboard/agents-dashboard.tsx",
    placeholder: /placeholder="Search agents\.\.\."/,
    label: "Search agents",
  },
  {
    file: "components/dashboard/memories-dashboard.tsx",
    placeholder: /placeholder="Search memories\.\.\."/,
    label: "Search memories",
  },
  {
    file: "components/dashboard/tasks-dashboard.tsx",
    placeholder: /placeholder="Search tasks\.\.\."/,
    label: "Search tasks",
  },
  {
    file: "components/dashboard/messages-dashboard.tsx",
    placeholder: /placeholder="subject, sender, recipient, content/,
    label: "Search messages",
  },
  {
    file: "components/dashboard/prompt-book-dashboard.tsx",
    placeholder: /placeholder="Search prompts by title/,
    label: "Search prompts",
  },
]

describe("AX-3: list-dashboard search inputs expose an accessible name", () => {
  for (const { file, placeholder, label } of CASES) {
    it(`${file} search box has aria-label="${label}"`, () => {
      const src = read(file)
      // The placeholder anchors us to the right Input.
      expect(placeholder.test(src)).toBe(true)
      expect(src.includes(`aria-label="${label}"`)).toBe(true)
    })
  }
})
