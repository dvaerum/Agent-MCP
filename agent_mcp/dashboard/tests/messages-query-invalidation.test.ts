// @vitest-environment jsdom
/**
 * W6-followup F3 — messages list on TanStack Query: SSE invalidation guard.
 *
 * The messages list is its own query (`['messages', project, {filters,
 * limit, offset}]`, fetched from `POST /messages/query` — NOT part of the
 * `/all-data` envelope). This pins:
 *
 *   1. `messagesQueryKey` shape + `invalidateMessages()` prefix-matching
 *      every page + filter variant of the messages list.
 *   2. A `resources/updated` SSE notification drives, after the 300ms
 *      debounce, exactly ONE messages invalidation — and a burst of
 *      notifications coalesces to a single refetch (the property the
 *      retired per-page `mcp:resources-updated` listener + 60s
 *      `setInterval` used to approximate, now on the shared choke point).
 */
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest"
import {
  queryClient,
  messagesQueryKey,
  invalidateMessages,
} from "@/lib/query-client"
import { dispatchNotification } from "@/lib/mcp-notifications"
import { projectContext } from "@/lib/project-context"

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  queryClient.clear()
})

describe("messagesQueryKey / invalidateMessages", () => {
  it("keys on [messages, project, params]", () => {
    const key = messagesQueryKey("proj", {
      filters: { read: false },
      limit: 100,
      offset: 0,
    })
    expect(key[0]).toBe("messages")
    expect(key[1]).toBe("proj")
    expect(key[2]).toEqual({ filters: { read: false }, limit: 100, offset: 0 })
  })

  it("defaults the project segment to 'standalone' and params to {}", () => {
    const key = messagesQueryKey(null)
    expect(key).toEqual(["messages", "standalone", {}])
  })

  it("invalidateMessages() prefix-matches every page + filter variant", async () => {
    const project = projectContext.projectName
    // Seed three cache entries: two pages of the unfiltered list + a
    // filtered variant. All must be invalidated by one call.
    queryClient.setQueryData(
      messagesQueryKey(project, { filters: {}, limit: 100, offset: 0 }),
      { messages: [], total: 0 },
    )
    queryClient.setQueryData(
      messagesQueryKey(project, { filters: {}, limit: 100, offset: 100 }),
      { messages: [], total: 0 },
    )
    queryClient.setQueryData(
      messagesQueryKey(project, {
        filters: { read: false },
        limit: 100,
        offset: 0,
      }),
      { messages: [], total: 0 },
    )
    // Fresh after a setQueryData write.
    for (const q of queryClient
      .getQueryCache()
      .findAll({ queryKey: ["messages"] })) {
      expect(q.isStale()).toBe(false)
    }

    await invalidateMessages()

    const msgQueries = queryClient
      .getQueryCache()
      .findAll({ queryKey: ["messages"] })
    expect(msgQueries.length).toBe(3)
    for (const q of msgQueries) {
      expect(q.isStale()).toBe(true)
    }
  })
})

describe("SSE resources/updated → messages invalidation", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("invalidates the messages key once after the 300ms debounce", () => {
    const spy = vi.spyOn(queryClient, "invalidateQueries")

    dispatchNotification({
      method: "notifications/resources/updated",
      params: { uri: "agent-mcp://inbox/x" },
    })
    // Nothing before the debounce elapses.
    expect(spy).not.toHaveBeenCalled()

    vi.advanceTimersByTime(300)

    const msgInvalidations = spy.mock.calls.filter(
      ([arg]) =>
        Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "messages",
    )
    expect(msgInvalidations.length).toBe(1)
  })

  it("coalesces a burst of notifications into a single messages refetch", () => {
    const spy = vi.spyOn(queryClient, "invalidateQueries")

    // A tight succession — the debounce must collapse these to one tick.
    for (let i = 0; i < 5; i++) {
      dispatchNotification({
        method: "notifications/resources/updated",
        params: { uri: `agent-mcp://inbox/agent-${i}` },
      })
    }
    vi.advanceTimersByTime(300)

    const msgInvalidations = spy.mock.calls.filter(
      ([arg]) =>
        Array.isArray((arg as { queryKey?: unknown[] })?.queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "messages",
    )
    expect(msgInvalidations.length).toBe(1)
  })
})
