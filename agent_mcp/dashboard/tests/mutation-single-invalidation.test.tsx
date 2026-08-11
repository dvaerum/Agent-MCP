// @vitest-environment jsdom
//
// W6-followup-2 G1 — single `/all-data` invalidation per mutation.
//
// The `/all-data`-backed pages (Memories, Agents) used to `await
// refreshData()` in every create/update/delete success handler AND still
// receive the backend `resources/updated` echo the SSE choke point turns
// into `invalidateAllData()` — TWO `/all-data` refetches per mutation.
//
// The fix routes the handler's own post-write signal through the SAME
// debounced choke point (`scheduleDashboardRefresh`) the echo uses, so:
//
//   1. operator mutation + backend echo coalesce into ONE all-data
//      invalidation (the debounce timer is a singleton), and
//   2. when SSE is down (no echo arrives), the handler's own signal STILL
//      fires exactly one invalidation — freshness is preserved without a
//      second fetch.
//
// This mirrors `all-data-query.test.tsx`'s burst-coalescing guard but
// pins the mutation-driven single-fetch invariant specifically.
import { describe, it, expect, vi, afterEach } from "vitest"
import { queryClient } from "@/lib/query-client"
import {
  dispatchNotification,
  scheduleDashboardRefresh,
} from "@/lib/mcp-notifications"

/** All-data-keyed invalidations recorded by the spy (F2/F3 also invalidate
 *  the sibling tasks/messages keys on the same tick, so we count the
 *  all-data key specifically). */
function allDataInvalidationCount(
  spy: ReturnType<typeof vi.spyOn>,
): number {
  return spy.mock.calls.filter(
    ([arg]) =>
      Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
      (arg as { queryKey: unknown[] }).queryKey[0] === "all-data",
  ).length
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  queryClient.clear()
})

describe("single /all-data invalidation per mutation (G1)", () => {
  it("coalesces an operator mutation + its backend echo into ONE all-data invalidation", () => {
    vi.useFakeTimers()
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries")

    // The operator creates a memory: the success handler signals the
    // shared choke point instead of an immediate un-coalesced refetch.
    scheduleDashboardRefresh()
    // The backend logs the write and fans out the matching echo to this
    // same operator's SSE stream.
    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://memories" },
    })

    // Debounced — nothing fires synchronously.
    expect(invalidateSpy).not.toHaveBeenCalled()

    vi.advanceTimersByTime(300)

    // Exactly one all-data refetch for the whole mutation, not two.
    expect(allDataInvalidationCount(invalidateSpy)).toBe(1)
  })

  it("still fires exactly ONE all-data invalidation when SSE is down (no echo)", () => {
    vi.useFakeTimers()
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries")

    // SSE stream is down: no `resources/updated` echo will arrive. The
    // operator's own mutation signal must still refresh the list once —
    // this is the freshness property that removing the imperative refresh
    // outright would have lost.
    scheduleDashboardRefresh()

    expect(invalidateSpy).not.toHaveBeenCalled()
    vi.advanceTimersByTime(300)

    expect(allDataInvalidationCount(invalidateSpy)).toBe(1)
  })
})
