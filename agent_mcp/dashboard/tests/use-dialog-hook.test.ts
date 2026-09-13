/**
 * Regression guards for the useDialog<T>() hook + dashboard migration.
 *
 * Candidate F1 from the 2026-06-01 architecture review: ~12 dashboard
 * dialogs each maintained their own ad-hoc state machine, typically the
 * pair ``useState<boolean>(false)`` for "open" plus ``useState<T |
 * null>(null)`` for the row being viewed/edited/deleted. That triplet
 * of ``open`` / ``data`` / ``setOpen+setData`` plumbing was repeated
 * across tasks-, agents-, memories-, messages-, and prompt-book-
 * dashboard, each with its own ad-hoc naming. Candidate F1 collapses
 * the duplication behind a single generic hook
 * ``hooks/use-dialog.ts::useDialog<T>()`` that returns
 * ``{isOpen, data, open, close}``.
 *
 * These tests are text-parse regression guards (same convention as
 * test_dashboard_messages_detail_popup.py / test_dashboard_tasks_row_icons.py,
 * now ported to this vitest convention); the fork has no jsdom
 * infrastructure, so behaviour is verified by ``npm run build`` +
 * manual click-through in the live dashboard.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

const HOOK_PATH = resolve(DASHBOARD_ROOT, "hooks", "use-dialog.ts")

function walkTsx(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = resolve(dir, name)
    if (statSync(p).isDirectory()) out.push(...walkTsx(p))
    else if (/\.tsx$/.test(name)) out.push(p)
  }
  return out
}

describe("useDialog<T>() hook", () => {
  // ---------- The hook itself --------------------------------------

  it("has hooks/use-dialog.ts exist as the home of the generic hook", () => {
    expect(existsSync(HOOK_PATH), `expected hook at ${HOOK_PATH}`).toBe(true)
  })

  it("exports a generic function useDialog<T>", () => {
    const src = read("hooks/use-dialog.ts")
    // Generic, exported, named useDialog.
    expect(
      /export\s+function\s+useDialog\s*</.test(src),
      "expected `export function useDialog<T>(...)` in hooks/use-dialog.ts",
    ).toBe(true)
    // Must be implemented on top of React's useState (the whole point
    // is that consumers stop doing it themselves).
    expect(
      src.includes("useState"),
      "expected the hook to use React's useState internally",
    ).toBe(true)
  })

  it("returns the canonical shape isOpen/data/open/close", () => {
    const src = read("hooks/use-dialog.ts")
    for (const member of ["isOpen", "data", "open", "close"]) {
      expect(
        src.includes(member),
        `expected hook to expose \`${member}\` on its return value`,
      ).toBe(true)
    }
  })

  // ---------- At least one consumer imports the hook ---------------

  it("has at least one consumer under components/dashboard/ import the hook — proving the migration started, not just the file", () => {
    const components = resolve(DASHBOARD_ROOT, "components", "dashboard")
    const importers = walkTsx(components).filter((p) => {
      const text = readFileSync(p, "utf8")
      return text.includes("useDialog") && text.includes("use-dialog")
    })
    expect(
      importers.length > 0,
      "expected at least one component under components/dashboard/ to " +
        "import useDialog from '@/hooks/use-dialog'",
    ).toBe(true)
  })

  // ---------- Negative assertion: legacy pair retired --------------
  //
  // We pick agents-dashboard.tsx as the canary because it had the
  // heaviest concentration of the legacy pair (5 separate dialogs each
  // with its own bool+null pair: detail / edit / terminate / purge /
  // task). After migration the entire ``useState<boolean>(false)`` line
  // pattern (paired with a sibling ``useState<X | null>(null)``) must
  // be gone from this file. Other migrated files are spot-checked in
  // their own PRs.

  it("no longer has the legacy dialog pair in agents-dashboard.tsx — it used to declare 5 setXxxDialogOpen boolean flags alongside the corresponding setSelectedXxx nullable row holders; the migration retires every one of them", () => {
    const src = read("components/dashboard/agents-dashboard.tsx")
    // The five tell-tale state-setter names from the pre-migration file.
    const forbidden = [
      "setTaskDialogOpen",
      "setPurgeDialogOpen",
      "setTerminateDialogOpen",
      "setEditDialogOpen",
      "setDetailDialogOpen",
    ]
    const leaked = forbidden.filter((name) => src.includes(name))
    expect(
      leaked,
      `expected the legacy \`setXxxDialogOpen\` boolean setters to be ` +
        `retired in favour of useDialog; still present: ${leaked}`,
    ).toEqual([])
    // And the hook itself must be present — i.e. we didn't just delete
    // the state and leave the dialogs floating.
    expect(
      src.includes("useDialog"),
      "expected agents-dashboard.tsx to use useDialog after migration",
    ).toBe(true)
  })
})
