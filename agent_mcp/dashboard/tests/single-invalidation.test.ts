/**
 * W6-followup-2 G1 — single `/all-data` invalidation per mutation.
 *
 * The `/all-data`-backed pages (Memories, Agents) used to `await
 * refreshData()` in every create/update/delete success handler AND still
 * receive the backend `resources/updated` echo the SSE choke point turns
 * into `invalidateAllData()` — TWO `/all-data` refetches per mutation.
 *
 * The fix routes each handler's own post-write signal through the SAME
 * debounced choke point the echo uses (`scheduleDashboardRefresh`), so the
 * operator's own write coalesces with the echo into exactly ONE refetch.
 * These grep guards pin the convention so a future edit doesn't reintroduce
 * the imperative refetch (which would restore the double-fetch):
 *
 *   - the mutation handlers call `scheduleDashboardRefresh()`;
 *   - they do NOT call `refreshData()` imperatively (`await`/`void`) — that
 *     hook is reserved for the manual Refresh button (`onRefresh`).
 *
 * The behavioural counterpart (the two signals coalescing into one
 * invalidation) is asserted in the vitest suite
 * `tests/mutation-single-invalidation.test.tsx`.
 *
 * Ported from tests/test_dashboard_single_invalidation.py (Python
 * source tree retired). The Python original used
 * `@pytest.mark.parametrize("src_name", ["memories", "agents"])` over
 * two of its four test functions — ported here as `it.each` to
 * preserve the same six total assertions-as-test-cases.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { agentsPageSource } from "./support/agents-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

const MEMORIES_PAGE = "components/dashboard/memories-dashboard.tsx"

const memoriesSrc = read(MEMORIES_PAGE)
const agentsSrc = agentsPageSource()

const pages: Array<["memories" | "agents", string]> = [
  ["memories", memoriesSrc],
  ["agents", agentsSrc],
]

describe("mutation handlers route through the shared choke point", () => {
  it("Memories mutation handlers call scheduleDashboardRefresh()", () => {
    expect(
      memoriesSrc.includes("scheduleDashboardRefresh"),
      "Memories mutation handlers must signal via the shared debounced " +
        "`scheduleDashboardRefresh()` choke point so the operator's write " +
        "coalesces with the backend echo into ONE /all-data refetch.",
    ).toBe(true)
  })

  it("Agents mutation handlers call scheduleDashboardRefresh()", () => {
    expect(
      agentsSrc.includes("scheduleDashboardRefresh"),
      "Agents mutation handlers must signal via the shared debounced " +
        "`scheduleDashboardRefresh()` choke point (was `await refreshData()`).",
    ).toBe(true)
  })
})

describe.each(pages)("%s: no imperative /all-data refetch in mutation handlers", (name, src) => {
  it("does not call `await refreshData()`", () => {
    // The imperative `/all-data` refetch (`await refreshData()` /
    // `void refreshData()`) is the double-fetch source. It must be gone
    // from the mutation paths; `refreshData` survives ONLY as the manual
    // `onRefresh` button binding.
    expect(
      src.includes("await refreshData()"),
      `${name}: \`await refreshData()\` reintroduces the double ` +
        "/all-data fetch — signal via `scheduleDashboardRefresh()` instead.",
    ).toBe(false)
  })

  it("does not call `void refreshData()`", () => {
    expect(
      src.includes("void refreshData()"),
      `${name}: \`void refreshData()\` reintroduces the double ` +
        "/all-data fetch — signal via `scheduleDashboardRefresh()` instead.",
    ).toBe(false)
  })
})

describe.each(pages)("%s: manual refresh button preserved", (name, src) => {
  it("keeps onRefresh wired to refreshData", () => {
    expect(
      src.includes("onRefresh: refreshData"),
      `${name}: the manual Refresh button (\`onRefresh: refreshData\`) ` +
        "must stay wired to the awaitable force-refetch.",
    ).toBe(true)
  })
})
