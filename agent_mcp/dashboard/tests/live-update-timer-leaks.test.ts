// @vitest-environment jsdom
/**
 * Timer-leak + single-invalidation guards for the dashboard's
 * live-update wiring.
 *
 * 1. Importing `lib/stores/data-store` must not arm a timer. The old
 *    60s freshness poll was a module-scope `setInterval` fired at import
 *    time with its handle thrown away — unstoppable by construction.
 *    Wave 6 removed the poll entirely (it now lives on the `/all-data`
 *    TanStack Query's `refetchInterval`), so importing the store must
 *    remain timer-free.
 *
 * 2. The operator-events subscription is the single mutation choke
 *    point: on connect it fires a catch-up `resources/updated`, which
 *    (after the 300ms debounce) calls `invalidateAllData()` — ONE
 *    `queryClient.invalidateQueries` for the shared query. Stopping the
 *    stream inside the debounce window must cancel that pending
 *    invalidation (no stray refetch against a store nobody renders); a
 *    stream left running must let it through.
 *
 * 3. The stream lifecycle drives the PF-3 `sseHealthy` flag — true on a
 *    successful connect, false again on stop.
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { useDataStore } from "@/lib/stores/data-store"
import { openMcpNotificationStream } from "@/lib/mcp-notifications"
import { queryClient } from "@/lib/query-client"

const realFetch = globalThis.fetch

describe("data-store import is timer-free", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("arms no timer merely by importing the module", async () => {
    vi.resetModules()
    vi.useFakeTimers()
    // Fake timers are installed BEFORE the (re-)import so any
    // module-scope setInterval/setTimeout is intercepted and countable.
    await import("@/lib/stores/data-store")
    expect(
      vi.getTimerCount(),
      "importing lib/stores/data-store armed a timer nobody can clear",
    ).toBe(0)
  })
})

describe("operator events subscription → single invalidation", () => {
  afterEach(() => {
    vi.useRealTimers()
    globalThis.fetch = realFetch
    vi.restoreAllMocks()
  })

  it("cancels the pending debounced invalidation when the stream stops", async () => {
    // `shouldAdvanceTime` keeps the fake clock creeping forward with
    // real time so the `await`s below (real microtask/macrotask turns
    // inside the fetch/ReadableStream plumbing) still resolve, while
    // leaving `advanceTimersByTime` in control of the 300ms debounce.
    vi.useFakeTimers({ shouldAdvanceTime: true })

    globalThis.fetch = vi.fn(
      async () =>
        new Response(new ReadableStream({ start: (c) => c.close() }), {
          status: 200,
        }),
    ) as unknown as typeof fetch

    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries")

    const handle = openMcpNotificationStream({ url: "/api/events" })
    // Let the run loop connect and fire its catch-up `resources/updated`,
    // which arms the 300ms debounce.
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1)

    handle.stop()

    await vi.advanceTimersByTimeAsync(1000)
    expect(
      invalidateSpy,
      "a stopped subscription still drove an invalidation 300ms later",
    ).not.toHaveBeenCalled()
  })

  it("invalidates once when the stream is left running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    globalThis.fetch = vi.fn(
      async () =>
        new Response(new ReadableStream({ start: (c) => c.close() }), {
          status: 200,
        }),
    ) as unknown as typeof fetch

    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries")

    const handle = openMcpNotificationStream({ url: "/api/events" })
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1000)

    expect(
      invalidateSpy,
      "the connect catch-up must invalidate the shared query",
    ).toHaveBeenCalled()
    handle.stop()
  })

  // PF-3: the stream lifecycle drives the data-store's sseHealthy flag —
  // true on a successful connect, false again on stop.
  it("marks SSE healthy on connect and unhealthy on stop", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    // A body that stays open so the reader doesn't hit `done` and
    // schedule a reconnect (which would flip health back to false).
    globalThis.fetch = vi.fn(
      async () =>
        new Response(new ReadableStream({ start: () => {} }), {
          status: 200,
        }),
    ) as unknown as typeof fetch

    useDataStore.setState({ sseHealthy: false })

    const handle = openMcpNotificationStream({ url: "/api/events" })
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(useDataStore.getState().sseHealthy).toBe(true)

    handle.stop()
    expect(useDataStore.getState().sseHealthy).toBe(false)
  })
})
