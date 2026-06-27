/**
 * Regression guard for the GET /mcp 405-spam loop (verify-all-v8,
 * tailnet target, 2026-06-27).
 *
 * Bug
 * ---
 * Wave 2 (cleanup-wave-2) stripped the router-side cookie→admin-bearer
 * translation from the GET /mcp path. PR #220 then closed the resulting
 * 500 (``session_registry_no_agent``) with a clean 405 at the router.
 *
 * The dashboard's ``McpNotificationsProvider`` still subscribed to that
 * endpoint with cookie-only auth at every project page load, so every
 * subscription attempt got 405 and the reconnect loop (capped at 30s,
 * but running for every visibilitychange and after every drop)
 * generated continuous 405s in the user's browser network tab. A user
 * reproduction on https://nixos-developer-system.tailfdae0.ts.net/
 * agent-mcp/app/washing-brothers/?page=memories showed 60+ ``GET
 * /agent-mcp/mcp/washing-brothers => 405`` lines within seconds of
 * page load.
 *
 * PR #220's bg-agent report explicitly flagged the dashboard's
 * GET-SSE subscription as "silently failing"; this test pins the fix
 * so a future refactor can't re-enable the polling without an explicit
 * test update + a working cookie-authenticated SSE endpoint behind it.
 *
 * Contract
 * --------
 * ``subscribeMcpNotifications()`` must NOT fire any HTTP request at
 * mount time. There is no cookie-authenticated SSE notification
 * endpoint on the backend right now; subscribing to a POST-only route
 * is dead code that only generates 405-spam.
 *
 * When a notification endpoint that accepts cookie auth is
 * (re)introduced — e.g. a dedicated ``/agent-mcp/api/<name>/
 * notifications`` SSE route — this test must be updated to assert the
 * new endpoint shape and ``credentials: include`` semantics. The
 * dispatch logic (``dispatchNotification``) is intentionally kept
 * exported so that future wiring can plug back in without rewriting
 * the JSON-RPC→store invalidation glue.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

describe("mcp-notifications no-poll regression guard", () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it("subscribeMcpNotifications() does NOT fire fetch on mount", async () => {
    // Stub fetch; if any request goes out, capture it for the
    // assertion to display.
    const captured: { url: string; init?: RequestInit }[] = []
    const fetchStub = vi.fn(async (url: string, init?: RequestInit) => {
      captured.push({ url, init })
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

    const mod = await import("@/lib/mcp-notifications")
    const unsubscribe = mod.subscribeMcpNotifications()

    // Yield a few microtask + macrotask ticks so any async fetch
    // scheduled by the run loop has a chance to fire. The pre-fix
    // implementation kicks off ``void run()`` synchronously inside
    // ``openMcpNotificationStream``; one tick is enough to surface
    // it, two is belt-and-braces.
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))

    unsubscribe()

    expect(
      captured.length,
      `subscribeMcpNotifications fired ${captured.length} request(s); ` +
        `none expected. Captured: ${JSON.stringify(captured.map((c) => c.url))}`,
    ).toBe(0)
    expect(fetchStub).not.toHaveBeenCalled()
  })

  it("subscribeMcpNotifications() returns a callable cleanup", async () => {
    const mod = await import("@/lib/mcp-notifications")
    const unsubscribe = mod.subscribeMcpNotifications()
    expect(typeof unsubscribe).toBe("function")
    // Calling the cleanup must not throw even when nothing was opened.
    expect(() => unsubscribe()).not.toThrow()
  })

  it("McpNotificationsProvider source does not pull in an active polling helper", () => {
    // Defence in depth: if a future refactor re-introduces a polling
    // import in the provider, this assertion flags it before the
    // runtime test even runs. The provider may still call
    // ``subscribeMcpNotifications`` (a no-op today) — what we forbid
    // is the provider opening a stream directly, which would bypass
    // the contract above.
    const src = read("components/providers/mcp-notifications-provider.tsx")
    expect(
      /openMcpNotificationStream\s*\(/.test(src),
      "McpNotificationsProvider must not call openMcpNotificationStream " +
        "directly (it bypasses the no-poll contract).",
    ).toBe(false)
    expect(
      /new\s+EventSource\s*\(/.test(src),
      "McpNotificationsProvider must not construct an EventSource " +
        "(no cookie-authenticated SSE endpoint exists right now).",
    ).toBe(false)
  })
})
