// @vitest-environment jsdom
/**
 * W6-followup F2 — tasks list on TanStack Query: SSE invalidation guard.
 *
 * The tasks list is its own query (`['tasks', project, filters]`, fetched
 * from `GET /tasks` — NOT part of the `/all-data` envelope). This pins:
 *
 *   1. `tasksQueryKey` shape + `invalidateTasks()` prefix-matching every
 *      filter variant of the tasks list.
 *   2. A `resources/updated` SSE notification drives, after the 300ms
 *      debounce, exactly ONE tasks invalidation — and a burst of
 *      notifications coalesces to a single refetch (the property the
 *      retired per-page `mcp:resources-updated` listener + 60s
 *      `setInterval` used to approximate, now on the shared choke point).
 */
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest"
import {
  queryClient,
  tasksQueryKey,
  invalidateTasks,
} from "@/lib/query-client"
import { dispatchNotification } from "@/lib/mcp-notifications"
import { projectContext } from "@/lib/project-context"

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  queryClient.clear()
})

describe("tasksQueryKey / invalidateTasks", () => {
  it("keys on [tasks, project, filters]", () => {
    const key = tasksQueryKey("proj", { status: "pending" })
    expect(key[0]).toBe("tasks")
    expect(key[1]).toBe("proj")
    expect(key[2]).toEqual({ status: "pending" })
  })

  it("defaults the project segment to 'standalone' and filters to {}", () => {
    const key = tasksQueryKey(null)
    expect(key).toEqual(["tasks", "standalone", {}])
  })

  it("invalidateTasks() prefix-matches every filter variant at once", async () => {
    const project = projectContext.projectName
    // Seed three cache entries: the unfiltered list + two filtered
    // variants. All must be invalidated by one invalidateTasks() call.
    queryClient.setQueryData(tasksQueryKey(project, {}), [])
    queryClient.setQueryData(tasksQueryKey(project, { status: "pending" }), [])
    queryClient.setQueryData(
      tasksQueryKey(project, { assigned: true }),
      [],
    )
    // Fresh after a setQueryData write.
    for (const q of queryClient.getQueryCache().findAll({ queryKey: ["tasks"] })) {
      expect(q.isStale()).toBe(false)
    }

    await invalidateTasks()

    const tasksQueries = queryClient
      .getQueryCache()
      .findAll({ queryKey: ["tasks"] })
    expect(tasksQueries.length).toBe(3)
    for (const q of tasksQueries) {
      expect(q.isStale()).toBe(true)
    }
  })
})

describe("SSE resources/updated → tasks invalidation", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("invalidates the tasks key once after the 300ms debounce", () => {
    const spy = vi.spyOn(queryClient, "invalidateQueries")

    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://inbox/x" },
    })
    // Nothing before the debounce elapses.
    expect(spy).not.toHaveBeenCalled()

    vi.advanceTimersByTime(300)

    const tasksInvalidations = spy.mock.calls.filter(
      ([arg]) =>
        Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "tasks",
    )
    expect(tasksInvalidations.length).toBe(1)
  })

  it("coalesces a burst of notifications into a single tasks refetch", () => {
    const spy = vi.spyOn(queryClient, "invalidateQueries")

    // A tight succession — the debounce must collapse these to one tick.
    for (let i = 0; i < 5; i++) {
      dispatchNotification({
        method: "notifications/resources/updated",
        params: { uri: `agent-mcp://status/agent-${i}` },
      })
    }
    vi.advanceTimersByTime(300)

    const tasksInvalidations = spy.mock.calls.filter(
      ([arg]) =>
        Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "tasks",
    )
    expect(tasksInvalidations.length).toBe(1)
  })
})
