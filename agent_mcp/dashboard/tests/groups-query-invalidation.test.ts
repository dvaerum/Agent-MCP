// @vitest-environment jsdom
/**
 * W6-followup F4 — groups list on TanStack Query: cache + invalidation.
 *
 * The router-admin groups list is its own query (`['groups']`, fetched
 * from `GET /agent-mcp/api/router/groups`). Unlike tasks/messages it is
 * ROUTER-level, not per-project, so its key carries NO project segment,
 * and — because the cross-project overview has NO operator-events SSE
 * stream (`subscribeMcpNotifications` early-returns for `isOverview`) —
 * there is NO debounced SSE invalidation for it either. Freshness after a
 * group mutation (create / edit / add-member / delete) rides a manual
 * `invalidateGroups()` call in the mutation success handler instead.
 *
 * This pins:
 *   1. `groupsQueryKey()` shape — a bare `['groups']`, no project segment.
 *   2. A seeded groups cache entry is served fresh, and `invalidateGroups()`
 *      marks it stale (i.e. a mounted groups query refetches after a group
 *      mutation), which is the manual-after-mutation analog of the
 *      tasks/messages SSE choke point.
 *   3. `invalidateGroups()` does NOT touch unrelated queries (tasks).
 */
import { describe, expect, it, afterEach } from "vitest"
import {
  queryClient,
  groupsQueryKey,
  invalidateGroups,
  tasksQueryKey,
} from "@/lib/query-client"

afterEach(() => {
  queryClient.clear()
})

describe("groupsQueryKey / invalidateGroups", () => {
  it("keys the router-level groups list as a bare ['groups'] (no project segment)", () => {
    const key = groupsQueryKey()
    expect(key).toEqual(["groups"])
  })

  it("serves a seeded groups entry fresh, then invalidateGroups() marks it stale", async () => {
    queryClient.setQueryData(groupsQueryKey(), [])
    // Fresh immediately after a setQueryData write.
    const seeded = queryClient
      .getQueryCache()
      .findAll({ queryKey: groupsQueryKey() })
    expect(seeded.length).toBe(1)
    expect(seeded[0]!.isStale()).toBe(false)

    await invalidateGroups()

    const after = queryClient
      .getQueryCache()
      .findAll({ queryKey: groupsQueryKey() })
    expect(after.length).toBe(1)
    expect(after[0]!.isStale()).toBe(true)
  })

  it("invalidateGroups() leaves unrelated queries (tasks) untouched", async () => {
    queryClient.setQueryData(tasksQueryKey("proj", {}), [])
    queryClient.setQueryData(groupsQueryKey(), [])

    await invalidateGroups()

    const tasks = queryClient
      .getQueryCache()
      .findAll({ queryKey: ["tasks"] })
    expect(tasks.length).toBe(1)
    // The tasks query must stay fresh — invalidateGroups() prefix-matches
    // ['groups'] only.
    expect(tasks[0]!.isStale()).toBe(false)
  })
})
