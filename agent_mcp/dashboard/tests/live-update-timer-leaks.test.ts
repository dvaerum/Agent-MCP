// @vitest-environment jsdom
/**
 * Timer-leak guards for the dashboard's live-update wiring.
 *
 * Both assertions here came out of the `testTimeout` 5s→20s bump
 * investigation (fix/vitest-notifications-flake). Neither leak was the
 * cause of the timeout — that turned out to be in-test module-graph
 * import cost, see the docblock in `mcp-notifications-no-poll.test.ts`
 * — but both are real: a timer that outlives the thing that created it
 * keeps running in a browser tab the operator has navigated away from,
 * and keeps a vitest worker's event loop armed after the file that
 * armed it has finished.
 *
 * 1. Importing `lib/stores/data-store` must not arm a timer. The
 *    60s freshness poll used to be a module-scope `setInterval` fired
 *    at import time with its handle thrown away — unstoppable by
 *    construction, and re-armed on every module re-evaluation
 *    (`vi.resetModules()`, Next Fast Refresh). It is now owned by
 *    `startDataStoreAutoRefresh()`, whose caller holds the stop.
 *
 * 2. Stopping an operator-events subscription must cancel the pending
 *    debounced refetch. `dispatchNotification()` coalesces resource
 *    churn behind a 300ms timer; if the stream is torn down inside
 *    that window (unmount, navigate-away, tab-hidden) the timer used
 *    to survive and drive a `refreshData()` against a store nobody is
 *    rendering.
 */

import { describe, expect, it, vi, afterEach } from "vitest"
// Static imports so the module graph is transformed during collection
// rather than inside a timed `it()` — see the note in
// `mcp-notifications-no-poll.test.ts`. The one exception is the
// "arms no timer merely by importing" case below, which needs a fresh
// module EVALUATION under fake timers to mean anything, and therefore
// keeps its `vi.resetModules()` + dynamic import.
import { useDataStore, startDataStoreAutoRefresh } from "@/lib/stores/data-store"
import { openMcpNotificationStream } from "@/lib/mcp-notifications"

const realFetch = globalThis.fetch

/** Minimal populated envelope — the poll is a no-op without one. */
const seedData = () => ({
  agents: [],
  tasks: [],
  context: [],
  actions: [],
  file_metadata: [],
  file_map: {},
  timestamp: new Date().toISOString(),
})

describe("data-store auto-refresh poll", () => {
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

  it("polls on the caller's schedule and stops when the caller stops", () => {
    vi.useFakeTimers()
    const refreshData = vi.fn(async () => {})
    useDataStore.setState({ refreshData, data: seedData() })

    const stop = startDataStoreAutoRefresh()
    expect(vi.getTimerCount()).toBe(1)

    vi.advanceTimersByTime(60_000)
    expect(refreshData).toHaveBeenCalledTimes(1)

    stop()
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(180_000)
    expect(
      refreshData,
      "the poll kept firing after its owner stopped it",
    ).toHaveBeenCalledTimes(1)
  })

  it("is idempotent — a second start does not double the poll rate", () => {
    vi.useFakeTimers()
    const refreshData = vi.fn(async () => {})
    useDataStore.setState({ refreshData, data: seedData() })

    const stopA = startDataStoreAutoRefresh()
    const stopB = startDataStoreAutoRefresh()
    expect(vi.getTimerCount()).toBe(1)

    vi.advanceTimersByTime(60_000)
    expect(refreshData).toHaveBeenCalledTimes(1)

    stopA()
    stopB()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe("operator events subscription teardown", () => {
  afterEach(() => {
    vi.useRealTimers()
    globalThis.fetch = realFetch
  })

  it("cancels the pending debounced refetch when the stream stops", async () => {
    // `shouldAdvanceTime` keeps the fake clock creeping forward with
    // real time so the `await`s below (which sit on real microtask +
    // macrotask turns inside the fetch/ReadableStream plumbing) still
    // resolve, while leaving `advanceTimersByTime` in control of the
    // 300ms debounce we actually want to interrogate.
    vi.useFakeTimers({ shouldAdvanceTime: true })

    globalThis.fetch = vi.fn(
      async () =>
        new Response(new ReadableStream({ start: (c) => c.close() }), {
          status: 200,
        }),
    ) as unknown as typeof fetch

    const refreshData = vi.fn(async () => {})
    useDataStore.setState({ refreshData })

    const handle = openMcpNotificationStream({ url: "/api/events" })
    // Let the run loop connect and fire its catch-up
    // `resources/updated`, which arms the 300ms debounce.
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1)

    handle.stop()

    await vi.advanceTimersByTimeAsync(1000)
    expect(
      refreshData,
      "a stopped subscription still drove a refetch 300ms later",
    ).not.toHaveBeenCalled()
  })

  it("still refetches when the stream is left running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    globalThis.fetch = vi.fn(
      async () =>
        new Response(new ReadableStream({ start: (c) => c.close() }), {
          status: 200,
        }),
    ) as unknown as typeof fetch

    const refreshData = vi.fn(async () => {})
    useDataStore.setState({ refreshData })

    const handle = openMcpNotificationStream({ url: "/api/events" })
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1)
    await vi.advanceTimersByTimeAsync(1000)

    expect(
      refreshData,
      "the connect catch-up must still reach the store",
    ).toHaveBeenCalled()
    handle.stop()
  })
})
