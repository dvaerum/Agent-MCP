/**
 * Regression guards for the useFilters<T>() hook + dashboard migration.
 *
 * PR 4 of the 2026-06-09 architecture review series. Candidate:
 * ``useFilters<T>`` — single ownership of filter state across the three
 * dashboards that used to hand-roll the same pattern:
 *
 * - ``messages-dashboard.tsx`` — 6 filter fields (from / to / type /
 *   priority / read / q) plus the v5.0.26 "filter changed → reset
 *   pagination cursor to 0" effect.
 * - ``tasks-dashboard.tsx`` — search / status / priority triplet.
 * - ``agents-dashboard.tsx`` — search / status pair.
 *
 * Each dashboard previously declared its own ``useState`` for every
 * filter field, its own per-field updater inline at the JSX call-site,
 * and (in the case of messages) its own filter-watching ``useEffect``
 * that reset the pagination offset. The pattern was identical; the
 * modules didn't share it. The hook lives at
 * ``hooks/use-filters.ts`` and exports ``useFilters<T>`` returning the
 * canonical shape ``{filters, setFilter, clearAll, isActive}``.
 *
 * These tests are text-parse regression guards (same convention as
 * ``test_dashboard_use_dialog_hook.py``, now ported to this vitest
 * convention); the fork has no jsdom infrastructure, so behaviour is
 * verified by ``npm run build`` + manual click-through in the live
 * dashboard.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, existsSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

describe("useFilters<T>() hook", () => {
  // ---------- The hook itself --------------------------------------

  it("has hooks/use-filters.ts exist as the home of the generic hook", () => {
    const path = resolve(DASHBOARD_ROOT, "hooks", "use-filters.ts")
    expect(existsSync(path), `expected hook at ${path}`).toBe(true)
  })

  it("exports a generic function useFilters<T>", () => {
    const src = read("hooks/use-filters.ts")
    expect(
      /export\s+function\s+useFilters\s*</.test(src),
      "expected `export function useFilters<T>(...)` in hooks/use-filters.ts",
    ).toBe(true)
    // Implemented on top of React's useState (the whole point is that
    // consumers stop doing it themselves).
    expect(
      src.includes("useState"),
      "expected the hook to use React's useState internally",
    ).toBe(true)
    // Should also useCallback for stable setter identities (consumers
    // pass them down to memoised children).
    expect(
      src.includes("useCallback"),
      "expected the hook to use React's useCallback for stable setter identities",
    ).toBe(true)
  })

  it("returns the canonical shape filters/setFilter/clearAll/isActive", () => {
    const src = read("hooks/use-filters.ts")
    for (const member of ["filters", "setFilter", "clearAll", "isActive"]) {
      expect(
        src.includes(member),
        `expected hook to expose \`${member}\` on its return value`,
      ).toBe(true)
    }
  })

  it("accepts an onReset callback — messages-dashboard uses it to reset the pagination cursor whenever a filter changes (this preserves the v5.0.26 behaviour that used to live in a dedicated useEffect watching the filters object)", () => {
    const src = read("hooks/use-filters.ts")
    expect(
      src.includes("onReset"),
      "expected hook to accept an `onReset` callback for filter-change " +
        "side-effects (e.g. resetting a pagination cursor)",
    ).toBe(true)
  })

  it("takes an initial filters snapshot — it's both the starting state AND the target of clearAll, AND the baseline for isActive", () => {
    const src = read("hooks/use-filters.ts")
    expect(
      src.includes("initial"),
      "expected hook to take `initial` (start state + clearAll target " +
        "+ isActive baseline)",
    ).toBe(true)
  })

  it("derives isActive by comparing the live filters object against initial — not by tracking a separate boolean flag; comment + implementation should make this explicit so future refactors don't accidentally invert the semantics", () => {
    const src = read("hooks/use-filters.ts")
    // JSON.stringify is the documented strategy (filter shapes are
    // primitive-only across all three consumers).
    expect(
      src.includes("JSON.stringify"),
      "expected `isActive` to compare filters to initial via JSON.stringify " +
        "(documented strategy for primitive-only filter shapes)",
    ).toBe(true)
  })

  // ---------- Consumer migrations ----------------------------------

  it("has messages-dashboard.tsx import the hook after migration", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    expect(
      src.includes("useFilters"),
      "expected messages-dashboard.tsx to import useFilters after migration",
    ).toBe(true)
    expect(
      src.includes("use-filters"),
      "expected messages-dashboard.tsx to reference '@/hooks/use-filters'",
    ).toBe(true)
  })

  it("has tasks-dashboard.tsx import the hook after migration", () => {
    const src = read("components/dashboard/tasks-dashboard.tsx")
    expect(
      src.includes("useFilters"),
      "expected tasks-dashboard.tsx to import useFilters after migration",
    ).toBe(true)
    expect(
      src.includes("use-filters"),
      "expected tasks-dashboard.tsx to reference '@/hooks/use-filters'",
    ).toBe(true)
  })

  it("has agents-dashboard.tsx import the hook after migration", () => {
    const src = read("components/dashboard/agents-dashboard.tsx")
    expect(
      src.includes("useFilters"),
      "expected agents-dashboard.tsx to import useFilters after migration",
    ).toBe(true)
    expect(
      src.includes("use-filters"),
      "expected agents-dashboard.tsx to reference '@/hooks/use-filters'",
    ).toBe(true)
  })

  // ---------- Negative assertions: legacy pattern retired ----------

  it("no longer hand-rolls filter state in messages-dashboard.tsx — the legacy useState<Filters>(...) declaration must be gone; the hook owns the filter state now", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    // The legacy `useState<Filters>(...)` pattern was unique to
    // messages-dashboard.tsx; if it still appears, the migration is
    // incomplete.
    expect(
      /useState\s*<\s*Filters\s*>/.test(src),
      "expected `useState<Filters>(...)` to be retired in favour of useFilters",
    ).toBe(false)
  })

  it("no longer has the filter reset effect in messages-dashboard.tsx — the v5.0.26 useEffect(() => setCurrentOffset(0), [filters]) pattern must move into onReset on the hook; leaving the effect AND calling onReset would double-fire the reset", () => {
    const src = read("components/dashboard/messages-dashboard.tsx")
    // The hook's onReset is the new home for "filter changed → page 1".
    // The old useEffect that watched `[filters]` and called
    // setCurrentOffset(0) must be gone.
    const pattern =
      /useEffect\s*\(\s*\(\)\s*=>\s*\{[^}]*setCurrentOffset\s*\(\s*0\s*\)[^}]*\}\s*,\s*\[\s*filters\s*\]/s
    expect(
      pattern.test(src),
      "expected the legacy `useEffect(() => setCurrentOffset(0), [filters])` " +
        "to be retired — the hook's onReset callback now owns this behaviour",
    ).toBe(false)
  })

  it("no longer declares individual filter useStates in tasks-dashboard.tsx — it used to declare three sibling useStates: searchTerm / statusFilter / priorityFilter; after migration these must come from the hook, not from individual useState calls", () => {
    const src = read("components/dashboard/tasks-dashboard.tsx")
    // The three legacy state setters from the pre-migration file. All
    // three should be gone — the hook returns a single `setFilter`
    // that subsumes them.
    const forbidden = [
      "const [searchTerm, setSearchTerm]",
      "const [statusFilter, setStatusFilter]",
      "const [priorityFilter, setPriorityFilter]",
    ]
    const leaked = forbidden.filter((name) => src.includes(name))
    expect(
      leaked,
      `expected the legacy individual filter useStates to be retired ` +
        `in favour of useFilters; still present: ${leaked}`,
    ).toEqual([])
  })

  it("no longer declares individual filter useStates in agents-dashboard.tsx — it used to declare two sibling useStates: searchTerm / statusFilter; after migration these must come from the hook, not from individual useState calls", () => {
    const src = read("components/dashboard/agents-dashboard.tsx")
    const forbidden = [
      "const [searchTerm, setSearchTerm]",
      "const [statusFilter, setStatusFilter]",
    ]
    const leaked = forbidden.filter((name) => src.includes(name))
    expect(
      leaked,
      `expected the legacy individual filter useStates to be retired ` +
        `in favour of useFilters; still present: ${leaked}`,
    ).toEqual([])
  })
})
