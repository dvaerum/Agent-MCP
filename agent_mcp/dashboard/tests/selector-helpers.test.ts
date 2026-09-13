/**
 * Regression guards for PR-W1d (architecture deepening, Finding #7):
 * extracted `normalizeAgentId` and `selectTasks` helpers in the
 * dashboard's Zustand data-store.
 *
 * Background
 * ----------
 *
 * Three selectors in `agent_mcp/dashboard/lib/stores/data-store.ts` --
 * `getAgentTasks`, `getAgentActions`, `getAgentTaskAnalysis` -- each
 * duplicated the same Admin/admin/strip-prefix dance plus the same
 * `t.assigned_to === ...` predicate inline. PR #130 fixed an overcount
 * bug ("completed tasks shown as assigned") on the Agents row but the
 * fix only landed in `agents-dashboard.tsx`; the same buggy filter
 * pattern in `data-store.ts` was unchanged. This PR refactors the
 * three selectors to compose two helpers so the next bug-fix can be
 * applied in one place.
 *
 * The dashboard ships no jsdom / RTL runtime tests, so these are
 * text-parse guards in the same style as the rest of this suite.
 * End-to-end behaviour is verified by `npm run build` (clean) and a
 * Firefox-MCP smoke pass documented on the PR.
 *
 * Ported from tests/test_dashboard_selector_helpers.py (Python source
 * tree retired).
 */

import { describe, expect, it } from "vitest"
import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const DATA_STORE = resolve(DASHBOARD_ROOT, "lib/stores/data-store.ts")
const SELECTORS = resolve(DASHBOARD_ROOT, "lib/stores/selectors.ts")
// Wave 6 keystone increment 1 (2026-08-11): the `/all-data` envelope +
// its derived agent-tasks selector moved off the zustand data-store onto
// TanStack Query. The composed selector now lives here as the pure
// `selectAgentTasks` helper; the redundant `getAgentActions` /
// `getAgentTaskAnalysis` store selectors (no component consumed them)
// were dropped in the same move.
const ALL_DATA_QUERY = resolve(DASHBOARD_ROOT, "lib/queries/all-data.ts")

function read(p: string): string {
  return readFileSync(p, "utf8")
}

// The helpers may live in either data-store.ts or its sibling
// selectors.ts (the spec leaves the file split as an implementation
// choice) — combine both, tolerating selectors.ts not existing.
function combinedStoreSources(): string {
  const sources: string[] = []
  if (existsSync(SELECTORS)) sources.push(read(SELECTORS))
  sources.push(read(DATA_STORE))
  return sources.join("\n")
}

// ---------- The helpers exist as named exports ------------------------

describe("extracted selector helpers exist", () => {
  it("normalizeAgentId(agentId: string): string exists as a named export", () => {
    const blob = combinedStoreSources()
    expect(
      blob.includes("export function normalizeAgentId"),
      "expected `export function normalizeAgentId(agentId: string): string` " +
        "in lib/stores/data-store.ts or lib/stores/selectors.ts",
    ).toBe(true)
  })

  it("selectTasks(tasks, criteria) exists as a named export", () => {
    const blob = combinedStoreSources()
    expect(
      blob.includes("export function selectTasks"),
      "expected `export function selectTasks(tasks, criteria)` in " +
        "lib/stores/data-store.ts or lib/stores/selectors.ts",
    ).toBe(true)
  })

  it("the TaskCriteria type/interface declares assignedTo, statusIn, statusNotIn", () => {
    const blob = combinedStoreSources()
    expect(
      blob.includes("TaskCriteria"),
      "expected a `TaskCriteria` type/interface declaring the composable " +
        "filter shape — search lib/stores/{data-store,selectors}.ts",
    ).toBe(true)
    for (const key of ["assignedTo", "statusIn", "statusNotIn"]) {
      expect(
        blob.includes(key),
        `TaskCriteria must declare a \`${key}\` field — selectors that ` +
          `want to exclude terminal statuses (PR #130 fix) need ` +
          `statusNotIn to compose, and assignedTo is the primary ` +
          `axis of the three callers.`,
      ).toBe(true)
    }
  })
})

// ---------- The selectors compose, not duplicate ---------------------

describe("selectors compose the helpers instead of duplicating logic", () => {
  it("data-store.ts no longer duplicates the inline Admin/admin normalize predicate", () => {
    // The signature-Admin-pair predicate
    // `(normalizedAgentId === 'admin' && (t.assigned_to === 'Admin' ||
    // t.assigned_to === 'admin'))` was copy-pasted into three places in
    // data-store.ts. After PR-W1d that exact phrasing must not appear
    // inline in the file — the selectors must compose normalizeAgentId +
    // selectTasks instead. (The helper itself may use a similar
    // predicate; we just forbid the duplication in the call sites.)
    const src = read(DATA_STORE)
    const pattern =
      /normalizedAgentId\s*===\s*['"]admin['"]\s*&&\s*\(\s*t\.assigned_to\s*===\s*['"]Admin['"]\s*\|\|\s*t\.assigned_to\s*===\s*['"]admin['"]/g
    const matches = src.match(pattern) ?? []
    expect(
      matches,
      "data-store.ts still contains the inline Admin/admin assigned_to " +
        "predicate -- it should compose normalizeAgentId + selectTasks " +
        `from the helpers instead. Found ${matches.length} match(es).`,
    ).toEqual([])
  })

  it("the composed agent-tasks selector references the extracted helpers", () => {
    // Wave 6 relocated this selector from the zustand data-store's
    // getAgentTasks to the pure selectAgentTasks helper in
    // lib/queries/all-data.ts (the `/all-data` envelope moved onto
    // TanStack Query). The redundant getAgentActions /
    // getAgentTaskAnalysis store selectors — which no component
    // consumed — were dropped in the same move, so they are no longer
    // asserted here.
    expect(
      existsSync(ALL_DATA_QUERY),
      "expected lib/queries/all-data.ts to exist after the Wave 6 " +
        "/all-data envelope migration onto TanStack Query",
    ).toBe(true)
    const src = read(ALL_DATA_QUERY)
    // Implementation form: `export function selectAgentTasks(` with a
    // function body.
    const m = /export function selectAgentTasks\s*\(/.exec(src)
    expect(
      m,
      "selectAgentTasks implementation not found in lib/queries/all-data.ts",
    ).not.toBeNull()
    // The body spans until the next top-level export. A 1500-char window
    // comfortably covers it.
    const start = m!.index
    const body = src.slice(start, start + 1500)
    expect(
      body.includes("selectTasks") && body.includes("selectActions"),
      "selectAgentTasks body does not compose selectTasks + selectActions " +
        `— was it actually migrated to compose the helpers? Body window:\n` +
        `${body.slice(0, 400)}...`,
    ).toBe(true)
  })
})

// ---------- PR #130 fix is preserved when composed -------------------

describe("PR #130 terminal-status exclusion survives the refactor", () => {
  it("the store sources reference all three terminal status strings", () => {
    // The agents-dashboard row-pill fix from PR #130 must still hold
    // after the data-store refactor: any selector body that filters by
    // assigned_to AND is intended to be a 'currently to-do' list must
    // exclude terminal statuses. The simplest invariant we can assert:
    // the data-store source mentions all three terminal status strings
    // ('completed', 'cancelled', 'failed') at least once each. Either
    // as a default-statusNotIn list inside selectTasks, or as an
    // explicit pass-through inside getAgentTaskAnalysis's assignedTasks
    // branch.
    //
    // (The row-pill regression in agents-dashboard.tsx is covered by
    // the agents-popup-polish guard and stays as-is.)
    const blob = combinedStoreSources()
    for (const status of ["completed", "cancelled", "failed"]) {
      expect(
        blob.includes(`'${status}'`) || blob.includes(`"${status}"`),
        `expected terminal status ${JSON.stringify(status)} to be referenced in ` +
          `lib/stores/{data-store,selectors}.ts so the dashboard's ` +
          `PR #130 fix (open-tasks-only) composes through the new ` +
          `selectTasks helper`,
      ).toBe(true)
    }
  })
})
