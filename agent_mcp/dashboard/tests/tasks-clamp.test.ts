/**
 * PF-1 clamp regression guard (Wave 3 — Table a11y + perf).
 *
 * tasks-dashboard fetches ALL tasks via GET /tasks (no server-side
 * pagination) and used to hand the full `filteredTasks` to the table
 * unbounded — the one dashboard list that rendered without a ceiling.
 * This pins the client-side clamp: a PAGE_SIZE, a sliced page window
 * fed to the table, an offset that resets when the filters change, and
 * a pagination footer — the same shape messages-dashboard already uses.
 *
 * Grep-based for the same reason as tests/mobile-load.test.ts: the
 * property we owe is "the list is bounded", a property of the SOURCE
 * wiring, and the dashboard's dependency graph is too heavy to mount in
 * the node-only harness.
 */
import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const src = readFileSync(
  resolve(__dirname, "..", "components/dashboard/tasks-dashboard.tsx"),
  "utf8",
)

describe("tasks-dashboard client-side clamp", () => {
  it("defines a PAGE_SIZE ceiling", () => {
    expect(
      /const\s+PAGE_SIZE\s*=\s*\d+/.test(src),
      "tasks-dashboard.tsx must define a numeric PAGE_SIZE clamp.",
    ).toBe(true)
  })

  it("feeds the table a sliced page window, not the full filteredTasks", () => {
    // The paged window is a slice keyed on the offset (currentOffset or
    // its defensively-clamped safeOffset)…
    expect(
      /\.slice\(\s*(?:currentOffset|safeOffset)\s*,\s*(?:currentOffset|safeOffset)\s*\+\s*PAGE_SIZE\s*\)/.test(
        src,
      ),
      "tasks-dashboard.tsx must slice filteredTasks to [offset, offset+PAGE_SIZE).",
    ).toBe(true)
    // …and it is the paged variable — not the raw filteredTasks — that
    // reaches the table's `rows` prop.
    expect(
      /rows=\{pagedTasks\}/.test(src),
      "tasks-dashboard.tsx must pass the paged (bounded) list to <DataTablePage rows>.",
    ).toBe(true)
    expect(
      /rows=\{filteredTasks\}/.test(src),
      "tasks-dashboard.tsx must NOT pass the unbounded filteredTasks to rows.",
    ).toBe(false)
  })

  it("resets the offset when the filter set changes", () => {
    // A useEffect keyed on the filter inputs must zero currentOffset so
    // a narrowed list doesn't strand the user on a now-empty page.
    expect(
      /setCurrentOffset\(0\)/.test(src),
      "tasks-dashboard.tsx must reset currentOffset to 0 on a filter change.",
    ).toBe(true)
  })

  it("renders a pagination range label", () => {
    expect(
      /Showing /.test(src),
      "tasks-dashboard.tsx must render a 'Showing N–M of T' range label.",
    ).toBe(true)
  })
})
