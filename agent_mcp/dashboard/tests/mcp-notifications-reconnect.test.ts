// @vitest-environment jsdom
/**
 * Reconnect catch-up guard.
 *
 * The operator-events hub is fire-and-forget: a mutation published while
 * the dashboard's SSE stream is down (transport drop, router read-timeout,
 * tab hidden→visible reopen, backend restart) reaches zero subscribers
 * and is gone — there is no replay buffer. So on every SUCCESSFUL
 * (re)connect the client must synthesize a `resources/updated` to force a
 * full refetch, reconciling whatever changed during the gap. This pins
 * that behaviour so a future refactor can't silently drop it and leave
 * post-gap changes stale until the slow poll.
 */
import { describe, expect, it, vi, afterEach } from "vitest"
// Static import, not an in-test `await import()` — the module graph's
// transform+evaluation cost would otherwise be billed to `testTimeout`
// instead of the collection phase. See the note in
// `mcp-notifications-no-poll.test.ts` for the measurements.
import { subscribeMcpNotifications } from "@/lib/mcp-notifications"

const realFetch = globalThis.fetch

describe("operator events SSE reconnect catch-up", () => {
  afterEach(() => {
    globalThis.fetch = realFetch
  })

  it("dispatches a resources/updated catch-up on (re)connect", async () => {
    const fetchStub = vi.fn(async () =>
      new Response(
        // Body closes immediately: the run loop connects, fires the
        // catch-up, then reaches end-of-stream and schedules a reconnect
        // (cancelled by unsubscribe()).
        new ReadableStream({ start(c) { c.close() } }),
        { status: 200 },
      ),
    )
    globalThis.fetch = fetchStub as unknown as typeof fetch

    const seen: (string | undefined)[] = []
    const handler = (e: Event) =>
      seen.push((e as CustomEvent).detail?.uri)
    window.addEventListener("mcp:resources-updated", handler)

    const unsubscribe = subscribeMcpNotifications()
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))
    unsubscribe()
    window.removeEventListener("mcp:resources-updated", handler)

    expect(
      seen,
      `expected a reconnect catch-up; saw ${JSON.stringify(seen)}`,
    ).toContain("agent-mcp://reconnect")
  })
})
