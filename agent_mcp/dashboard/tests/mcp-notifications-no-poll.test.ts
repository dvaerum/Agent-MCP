/**
 * Contract guard for the operator live-update SSE subscription.
 *
 * History
 * -------
 * The GET /mcp 405-spam loop (verify-all-v8, 2026-06-27): the dashboard
 * subscribed to the agent-scoped ``GET /agent-mcp/mcp/<project>`` with
 * cookie-only auth, which the router rejects with 405 (that GET stream
 * derives ``agent_id`` from a per-agent bearer the cookie can't carry).
 * A user reproduction showed 60+ ``=> 405`` lines within seconds of a
 * project page load. The interim fix turned ``subscribeMcpNotifications``
 * into a no-op so no request fired at all.
 *
 * The real fix is a dedicated cookie-authenticated operator events
 * endpoint (``features/operator_events.py`` + ``GET /api/events``,
 * proxied as ``/agent-mcp/api/<name>/events``). This test now pins the
 * re-wired contract:
 *
 *   1. ``subscribeMcpNotifications()`` opens exactly one stream at mount,
 *      against the operator events endpoint — NOT the ``/mcp`` transport.
 *   2. The fetch carries ``credentials: "include"`` (cookie auth), a GET
 *      method, and an ``Accept: text/event-stream`` header.
 *   3. It returns a callable cleanup that stops the stream cleanly.
 *   4. ``McpNotificationsProvider`` never opens a stream directly — it
 *      only calls ``subscribeMcpNotifications`` (which owns the endpoint
 *      choice + reconnect/visibility lifecycle).
 */

import { describe, expect, it, vi, afterEach } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
// Imported statically, NOT with an in-test `await import()`.
//
// Why it matters: this module pulls in the data-store, the api client
// and zustand, and Vitest charges module transform + evaluation to
// whichever phase asks for it. Behind an in-test dynamic import the
// whole graph was billed to `testTimeout` — measured at 175ms idle but
// 1.3–3.1s when the box is oversubscribed, i.e. 85%+ of this test's
// wall time was module loading, not the assertion. That is what pushed
// the file over the old 5s budget under parallel load and prompted a
// 5s→20s bump. A static import moves the cost into the collection
// phase where it belongs. `vi.resetModules()` was ceremony — nothing
// here depends on fresh module state (the stream's state is per-call,
// held in `openMcpNotificationStream`'s closure).
import * as mcpNotifications from "@/lib/mcp-notifications"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

const realFetch = globalThis.fetch

describe("operator events SSE subscription contract", () => {
  afterEach(() => {
    globalThis.fetch = realFetch
  })

  it("subscribeMcpNotifications() opens one stream against /api/events", async () => {
    const captured: { url: string; init?: RequestInit }[] = []
    const fetchStub = vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init })
      // Immediately-closing body so the run loop finishes its first pass
      // (it will schedule a reconnect timer we cancel via unsubscribe()).
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.close()
          },
        }),
        { status: 200 },
      )
    })
    // @ts-expect-error global stub for the duration of this test
    globalThis.fetch = fetchStub

    const unsubscribe = mcpNotifications.subscribeMcpNotifications()

    // Yield a couple of ticks so the async ``void run()`` fires its fetch.
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))

    unsubscribe()

    expect(
      captured.length,
      `expected exactly one stream open; captured ` +
        `${JSON.stringify(captured.map((c) => c.url))}`,
    ).toBe(1)

    const { url, init } = captured[0]!
    // Targets the operator events channel, not the MCP transport.
    expect(url).toContain("/events")
    expect(url).not.toContain("/mcp")
    // Cookie auth + SSE framing.
    expect(init?.credentials).toBe("include")
    expect(init?.method).toBe("GET")
    const headers = (init?.headers ?? {}) as Record<string, string>
    expect(headers["Accept"]).toBe("text/event-stream")
  })

  it("subscribeMcpNotifications() returns a callable cleanup", () => {
    // Stub fetch so the opened stream doesn't hit undici with a relative
    // URL; the body closes immediately.
    const fetchStub = vi.fn(async () => new Response(
      new ReadableStream({ start(c) { c.close() } }),
      { status: 200 },
    ))
    globalThis.fetch = fetchStub as unknown as typeof fetch

    const unsubscribe = mcpNotifications.subscribeMcpNotifications()
    expect(typeof unsubscribe).toBe("function")
    // Calling the cleanup must not throw.
    expect(() => unsubscribe()).not.toThrow()
  })

  it("openMcpNotificationStream default URL targets the events endpoint", () => {
    // Source-level guard: the default stream URL builder must resolve to
    // the events channel, never the retired /mcp transport.
    const src = read("lib/mcp-notifications.ts")
    expect(src).toContain("eventsUrlForProject")
    expect(
      /options\.url\s*\?\?\s*eventsUrlForProject\(\)/.test(src),
      "openMcpNotificationStream must default to eventsUrlForProject()",
    ).toBe(true)
  })

  it("McpNotificationsProvider does not open a stream directly", () => {
    // The provider may call ``subscribeMcpNotifications`` — what we
    // forbid is it opening a stream directly (bypassing the endpoint +
    // lifecycle owned by mcp-notifications.ts).
    const src = read("components/providers/mcp-notifications-provider.tsx")
    expect(
      /openMcpNotificationStream\s*\(/.test(src),
      "McpNotificationsProvider must not call openMcpNotificationStream " +
        "directly.",
    ).toBe(false)
    expect(
      /new\s+EventSource\s*\(/.test(src),
      "McpNotificationsProvider must not construct an EventSource.",
    ).toBe(false)
  })
})
