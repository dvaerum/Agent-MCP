/**
 * Regression guards for the useDialog<T>() live-lookup refactor.
 *
 * Background (Candidate D, 2026-06-02 architecture review): the original
 * ``useDialog<T>()`` hook (Candidate F1 from 2026-06-01) stored a
 * *snapshot* of the row at ``open(row)`` time. Background refresh
 * updated the underlying zustand store, but the dialog kept rendering
 * against the captured snapshot. PR #74's Add-Note investigation traced
 * the user-visible "saved note disappears" symptom to exactly this:
 * after the Edit dialog saved a new note and the store updated, the
 * View dialog still rendered the pre-save object.
 *
 * The fix retires the snapshot. ``useDialog<T>`` now holds a **key**
 * (typically ``task.task_id``, ``message.message_id``, etc.) and a
 * **selector** function that the hook calls on every render to read the
 * current row from the live source. When the source is the zustand
 * data-store, the selector is a zustand subscription, so updates re-
 * render the dialog automatically. When the source is local component
 * state, the selector is a plain ``useCallback`` closure that re-runs
 * when the source array changes.
 *
 * These tests are text-parse regression guards (same convention as
 * ``test_dashboard_use_dialog_hook.py``, now ported to this vitest
 * convention); the fork has no jsdom infrastructure, so behaviour is
 * verified by ``npm run build`` plus a Firefox-MCP click-through in the
 * live dashboard.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const HOOK = resolve(DASHBOARD_ROOT, "hooks", "use-dialog.ts")
const COMPONENTS = resolve(DASHBOARD_ROOT, "components", "dashboard")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

function readHook(): string {
  return readFileSync(HOOK, "utf8")
}

function walkTsx(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = resolve(dir, name)
    if (statSync(p).isDirectory()) out.push(...walkTsx(p))
    else if (/\.tsx$/.test(name)) out.push(p)
  }
  return out
}

describe("useDialog<T>() live-lookup refactor", () => {
  // ---------- The hook: new shape ----------------------------------

  it("requires a selector argument on useDialog<T> — the whole point of the refactor is that the dialog no longer snapshots a row at open() time, it asks the selector for the current row on every render; a zero-arg useDialog<T>() would silently revert to snapshot mode and re-introduce the bug class", () => {
    const src = readHook()
    // Generic, exported, and the signature must take at least one
    // parameter (the selector). We tolerate either a single positional
    // selector or an options bag — both encode "user must supply a
    // lookup".
    const sig = src.match(
      /export\s+function\s+useDialog\s*<[^>]+>\s*\(\s*([^)]*)\)/,
    )
    expect(sig, "expected export function useDialog<...>(...)").not.toBeNull()
    const params = sig![1]!.trim()
    expect(
      params.length > 0,
      "useDialog must take a selector parameter; an empty argument " +
        "list re-introduces snapshot-mode behaviour",
    ).toBe(true)
  })

  it("stores a key, not a snapshot, in the hook's internal state — storing the whole row recreates the snapshot bug, even with a selector, a setState(row) hidden inside the hook would mean the dialog renders against that snapshot when the selector returns null", () => {
    const src = readHook()
    // Strip block / line comments so docstring prose about the old
    // snapshot shape doesn't trip the heuristic.
    let code = src.replace(/\/\*[\s\S]*?\*\//g, "")
    code = code.replace(/^\s*\/\/.*$/gm, "")
    // The state must be typed as a key (string | null is the standard
    // case; we accept any narrower union via "K | null").
    expect(
      /useState\s*<\s*[\w\s|]*\bnull\s*>\s*\(\s*null\s*\)/.test(code),
      "expected the hook to store a key with useState<K | null>(null)",
    ).toBe(true)
    // Negative: the hook implementation must NOT store the row itself.
    expect(
      code.replace(/ /g, "").includes("useState<T"),
      "useDialog must not store useState<T | null>; storing the row " +
        "is the snapshot bug we are fixing",
    ).toBe(false)
  })

  it("keeps exposing isOpen, data, open, close on the canonical shape", () => {
    const src = readHook()
    for (const member of ["isOpen", "data", "open", "close"]) {
      expect(
        src.includes(member),
        `expected hook to expose \`${member}\` on its return value`,
      ).toBe(true)
    }
  })

  it("documents the live-lookup contract in the hook header — without the docstring, future contributors might 'fix' the selector-only API back to snapshot-on-open ('simpler', they'll say) and regress the bug", () => {
    const src = readHook()
    // Look for either of the load-bearing words from the design note.
    expect(
      /(live|snapshot|selector)/i.test(src),
      "expected the hook docstring to mention live/selector/snapshot rationale",
    ).toBe(true)
  })

  // ---------- Consumers: no zero-arg call remains ------------------

  const LEGACY_ZERO_ARG = /useDialog\s*<[^>]+>\s*\(\s*\)/

  it("has no remaining call site use the old zero-argument form — each consumer MUST pass a selector so its dialog reads live data", () => {
    const offenders: string[] = []
    for (const path of walkTsx(COMPONENTS)) {
      const lines = readFileSync(path, "utf8").split("\n")
      lines.forEach((line, idx) => {
        if (LEGACY_ZERO_ARG.test(line)) {
          offenders.push(`${path}:${idx + 1}: ${line.trim()}`)
        }
      })
    }
    expect(
      offenders,
      "the following consumers still call useDialog<T>() with no " +
        `selector argument (snapshot-mode bug class):\n  ${offenders.join("\n  ")}`,
    ).toEqual([])
  })

  // ---------- Stale bandage removed --------------------------------

  it("removes the tasks-dashboard.tsx stale SelectItem bandage — it's dead after the refactor: the Edit dialog now reads the current task row, so its assigned_to is always in sync with the live agent roster and the workaround SelectItem can never fire", () => {
    const src = read("components/dashboard/tasks-dashboard.tsx")
    expect(
      src.includes("(stale)"),
      "tasks-dashboard.tsx still carries the '(stale)' bandage; the " +
        "live-lookup refactor was supposed to remove it",
    ).toBe(false)
  })

  // ---------- Per-consumer migration audit -------------------------
  //
  // Every consumer of useDialog<T> must pass a selector. This catches
  // half-migrations where a file was edited but one ``useDialog<X>()``
  // call slipped through.
  //
  // Map: file -> list of variable names that must appear with a
  // selector call.

  const EXPECTED_CONSUMERS: Record<string, string[]> = {
    "components/dashboard/tasks-dashboard.tsx": [
      "viewDialog",
      "editDialog",
      "deleteDialog",
    ],
    "components/dashboard/agents-dashboard.tsx": [
      "taskDialog",
      "purgeDialog",
      "terminateDialog",
      "editDialog",
      "detailDialog",
    ],
    "components/dashboard/memories-dashboard.tsx": ["viewDialog", "editDialog"],
    "components/dashboard/messages-dashboard.tsx": [
      "detailDialog",
      // Added by the Messages-page-parity PR: single-row / in-modal /
      // mobile delete all route through this confirm dialog instead of
      // firing an unconfirmed DELETE.
      "deleteDialog",
    ],
    // NOTE: overview-dashboard.tsx no longer carries a ``nodeDialog``
    // consumer. The mobile-load PR (#232) replaced the page's
    // ``<VisGraph>`` + ``<NodeDetailPanel>`` pair with summary stat
    // cards + a "View Collaboration Network" link to the System page.
    // The System page itself (and the vis-network graph feature
    // entirely) was later removed outright — nothing left in the
    // dashboard owns a graph/node-detail interaction, so there is no
    // replacement consumer to add here.
    "components/dashboard/prompt-book-dashboard.tsx": ["builderDialog"],
    // NOTE: agent-details-panel.tsx was deleted by the Messages-page-
    // parity PR — it was dead code (imported/rendered nowhere; the live
    // agent detail is <AgentDetailDialog> inside agents-dashboard.tsx).
    // Its former ``taskDialog`` consumer entry is removed accordingly.
  }

  it("has at least twelve consumers migrated — architecture review counted 12+ dialog consumers across the dashboard, make sure we didn't miss any", () => {
    const total = Object.values(EXPECTED_CONSUMERS).reduce(
      (sum, v) => sum + v.length,
      0,
    )
    expect(
      total >= 12,
      `expected at least 12 useDialog consumers to migrate; ` +
        `the audit map covers only ${total}`,
    ).toBe(true)
  })

  it("has every listed consumer pass a selector — each useDialog<X>(...) call site must pass a non-empty argument list (the selector); the zero-arg check above catches missing selectors globally, here we additionally verify the named variables we know about still exist + use useDialog", () => {
    const missing: string[] = []
    for (const [rel, varnames] of Object.entries(EXPECTED_CONSUMERS)) {
      const src = read(rel)
      for (const v of varnames) {
        // Match `const <var> = useDialog<...>(...)` — argument list
        // must contain at least one non-whitespace character.
        const pattern = new RegExp(
          `const\\s+${v}\\s*=\\s*useDialog\\s*<[^>]+>\\s*\\(\\s*\\S`,
        )
        if (!pattern.test(src)) {
          missing.push(`${rel}::${v}`)
        }
      }
    }
    expect(
      missing,
      "the following consumers either disappeared or still call " +
        `useDialog<T>() with no selector argument:\n  ${missing.join("\n  ")}`,
    ).toEqual([])
  })
})
