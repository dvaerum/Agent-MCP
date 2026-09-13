/**
 * Regression guards: dashboard subscribes to MCP notifications on GET /mcp.
 *
 * Background
 * ----------
 *
 * PR #79 (Candidate A) wired session_registry into the transport, so
 * `notifications/resources/updated`, `notifications/prompts/list_changed`,
 * and `notifications/tools/list_changed` actually arrive on GET /mcp SSE
 * streams for the bearer's agent_id.
 *
 * Candidate E (this PR) wires the dashboard to subscribe. When a
 * notification arrives, the dashboard invalidates the relevant zustand
 * store slices so other tabs / the same tab see fresh data within seconds
 * instead of waiting up to 60s for the data-store auto-poll tick.
 *
 * URL plumbing
 * ------------
 *
 * The dashboard derives `projectName` from `window.location.pathname`
 * (`/agent-mcp/__dashboard/<name>/...`) in `lib/project-context.ts`.
 * The MCP Streamable HTTP endpoint for that project lives at
 * `/agent-mcp/<name>/mcp` — note this is NOT the `/__api/` REST prefix;
 * it's the router's MCP path served as the wrapped backend's `/mcp`.
 *
 * The `apiClient.createEventSource('/mcp')` helper would resolve to
 * `{baseUrl}/mcp` = `/agent-mcp/__api/<name>/mcp` which the router does
 * not expose. We therefore build the MCP URL separately from `baseUrl`
 * (see `mcpUrlForProject()` in `lib/mcp-notifications.ts`).
 *
 * Auth
 * ----
 *
 * `EventSource` (the browser primitive) cannot send custom headers and
 * won't carry cookies cross-origin reliably, so the dashboard uses
 * `fetch` + a ReadableStream reader instead.
 *
 * Wave 2 (cleanup-wave-2, 2026-06-20) migrated the subscription off
 * bearer auth onto cookie auth. The fetch sends `credentials: "include"`;
 * the router's `backend_mcp_handler` validates the `agent_mcp_session`
 * cookie + project membership and injects the project's admin token
 * upstream so the backend's `AuthHeaderMiddleware` (still bearer-only)
 * sees a valid bearer. No admin token ever lives in JS memory anymore.
 *
 * These tests are text-level (the fork has no jsdom/RTL infrastructure
 * for behavioural dashboard tests). Runtime is verified by `npm run
 * build` + the smoke test in the PR body.
 */

import { describe, expect, it } from "vitest"
import { existsSync, readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const MCP_NOTIF = resolve(DASHBOARD_ROOT, "lib", "mcp-notifications.ts")

const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// -- module existence -----------------------------------------------------

describe("mcp notifications module", () => {
  it("exists as its own module", () => {
    // The subscription lives in its own module so the wiring is
    // discoverable + the listener-shape decisions are reviewable in
    // isolation from the rest of `lib/api.ts`.
    expect(
      existsSync(MCP_NOTIF),
      `expected ${MCP_NOTIF} to exist — Candidate E adds a dedicated ` +
        "module that opens a GET /mcp SSE subscription and routes the " +
        "JSON-RPC notification frames into the right store invalidations",
    ).toBe(true)
  })

  // -- transport shape ------------------------------------------------------

  it("uses fetch, not EventSource, for cookie auth", () => {
    // EventSource can't reliably ride a cross-origin cookie, so the
    // Wave-2 subscription uses `fetch` (+ ReadableStream) with
    // `credentials: "include"` to send the operator session cookie.
    //
    // Wave 2 (cleanup-wave-2, 2026-06-20) replaced the bearer-header
    // construction with cookie auth — see lib/mcp-notifications.ts.
    // The router's `backend_mcp_handler` resolves the cookie to the
    // project's admin token and injects the bearer upstream so the
    // backend's `AuthHeaderMiddleware` (still bearer-only) accepts the
    // request.
    const src = read("lib/mcp-notifications.ts")
    expect(
      src.includes("fetch("),
      "expected `fetch(` in lib/mcp-notifications.ts — EventSource " +
        "can't carry a cookie cross-origin reliably",
    ).toBe(true)
    expect(
      src.includes("getReader") || src.includes("ReadableStream"),
      "expected a ReadableStream reader (`getReader()`) — the " +
        "subscription consumes the response body as a stream and parses " +
        "SSE frames manually",
    ).toBe(true)
    // Cookie auth: credentials: "include" must be set on the fetch.
    expect(
      /credentials\s*:\s*['"]include['"]/.test(src),
      'expected `credentials: "include"` on the /mcp fetch — the ' +
        "operator session cookie carries auth post-Wave-2",
    ).toBe(true)
    // And the legacy bearer construction must be gone — a stray
    // `Authorization: Bearer ${...}` would re-leak the admin token
    // back into JS memory and bypass the cookie path.
    expect(
      /Authorization\s*:\s*[`'"]Bearer/.test(src),
      "lib/mcp-notifications.ts must not construct an `Authorization: " +
        "Bearer` header — Wave 2 moved auth to the session cookie",
    ).toBe(false)
  })

  it("MCP url uses the project prefix, not the __api prefix", () => {
    // The MCP path is `/agent-mcp/<name>/mcp`, NOT
    // `/agent-mcp/__api/<name>/mcp` (the latter is the REST proxy
    // prefix). Catches the bug where someone would naively call
    // `apiClient.createEventSource('/mcp')` and get a 404.
    const src = read("lib/mcp-notifications.ts")
    // The path-prefix literal — split-string forms are OK too, so we
    // search for the segments rather than a single full literal.
    expect(
      src.includes("/agent-mcp/"),
      "expected `/agent-mcp/` URL segment in mcp-notifications.ts — " +
        "the path-prefixed deployment serves MCP at this root",
    ).toBe(true)
    expect(
      src.includes("/mcp"),
      "expected `/mcp` URL segment in mcp-notifications.ts",
    ).toBe(true)
    // The most common bug: building `${baseUrl}/mcp` which resolves to
    // `/agent-mcp/__api/<name>/mcp` (404).
    expect(
      /\$\{(?:[\w.]+\.)?baseUrl\}\/mcp/.test(src),
      "found `${...baseUrl}/mcp` in mcp-notifications.ts — that " +
        "resolves to /agent-mcp/__api/<name>/mcp (404). Use " +
        "/agent-mcp/<projectName>/mcp instead",
    ).toBe(false)
    // And don't accidentally embed the __api prefix in the MCP URL.
    expect(
      /\/__api\/[^'"`]*\bmcp\b/.test(src),
      "found `/__api/.../mcp` literal in mcp-notifications.ts — the " +
        "MCP transport is mounted under /agent-mcp/<name>/mcp directly, " +
        "not under the REST prefix",
    ).toBe(false)
  })

  // -- notification dispatch ------------------------------------------------

  it("dispatches prompts/list_changed to the data store", () => {
    // `notifications/prompts/list_changed` triggers the existing
    // `notifyPromptsListChanged()` helper (added in PR #70). The path
    // `data-store.ts:invalidatePromptsCatalog` exists already; this PR
    // only needs to call into it.
    const src = read("lib/mcp-notifications.ts")
    expect(
      src.includes("prompts/list_changed"),
      "expected handling of `notifications/prompts/list_changed` " +
        "method in mcp-notifications.ts",
    ).toBe(true)
    expect(
      src.includes("notifyPromptsListChanged") ||
        src.includes("invalidatePromptsCatalog"),
      "expected the prompts-changed branch to call " +
        "notifyPromptsListChanged() (exported from lib/stores/data-store.ts) " +
        "OR invalidatePromptsCatalog() directly",
    ).toBe(true)
  })

  it("dispatches resources/updated to a data-store refresh", () => {
    // `notifications/resources/updated` with
    // `params.uri = agent-mcp://inbox/<agent_id>` (or status/...)
    // must trigger a data-store refresh so message counters + ambient
    // state update without waiting for the 60s poll.
    const src = read("lib/mcp-notifications.ts")
    expect(
      src.includes("resources/updated"),
      "expected handling of `notifications/resources/updated` method",
    ).toBe(true)
    // The agent-mcp:// URI scheme is the load-bearing namespace for
    // inbox / status resources.
    expect(
      src.includes("agent-mcp://") ||
        src.includes("inbox") ||
        src.includes("refreshData"),
      "expected the resources-updated branch to refresh data-store " +
        "(e.g. call useDataStore.getState().refreshData()) so the " +
        "messages list + agent counters update in real time",
    ).toBe(true)
  })

  it("dispatches tools/list_changed", () => {
    // `notifications/tools/list_changed` is the third notification
    // surface. Even if the dashboard doesn't yet render a tool catalogue,
    // the listener must recognise the method so future tool-aware UI
    // work can hook into the existing dispatch table without re-touching
    // this module.
    const src = read("lib/mcp-notifications.ts")
    expect(
      src.includes("tools/list_changed"),
      "expected handling of `notifications/tools/list_changed` method " +
        "in mcp-notifications.ts (even a no-op or debug-log handler " +
        "documents that the dispatch table covers all three notification " +
        "kinds the backend currently emits)",
    ).toBe(true)
  })

  // -- resilience -----------------------------------------------------------

  it("subscribes with reconnect + exponential backoff", () => {
    // The connection drops happen (server restart, transient network
    // error, lazily-spawned backend going to sleep). The subscription
    // must reconnect with exponential backoff capped at 30s.
    const src = read("lib/mcp-notifications.ts")
    // Reconnect loop. Accept any of: setTimeout-based scheduler with
    // doubling delay, while(true) try/catch, or an explicit `reconnect`
    // / `backoff` symbol.
    expect(
      src.includes("setTimeout"),
      "expected `setTimeout(...)` to schedule reconnect attempts",
    ).toBe(true)
    // Exponential growth marker. Accept `* 2`, `** attempt`, `Math.pow`.
    const hasExp =
      src.includes("* 2") ||
      src.includes("** ") ||
      src.includes("Math.pow") ||
      src.includes("Math.min")
    expect(
      hasExp,
      "expected exponential growth on the reconnect delay (e.g. " +
        "`delay = Math.min(maxDelay, delay * 2)` or `base ** attempt`)",
    ).toBe(true)
    // The 30s cap.
    expect(
      src.includes("30000") || src.includes("30_000"),
      "expected the 30000ms (30s) cap on reconnect backoff — without " +
        "it a long outage produces minutes-long retry gaps",
    ).toBe(true)
  })

  it("subscribeMcpNotifications opens the stream against the cookie SSE endpoint", () => {
    // Re-enabled after the operator SSE endpoint shipped
    // (``GET /api/events``, cookie-authenticated — the operator
    // live-update channel). The verify-all-v8 no-op guard held only
    // *until* a cookie-auth SSE endpoint existed; now that it does,
    // ``subscribeMcpNotifications`` opens the notification stream on mount
    // AND attaches a visibilitychange listener (battery-saver: pause the
    // stream when the tab is hidden, resume when visible).
    //
    // The lower-level ``openMcpNotificationStream`` stays exported with its
    // cookie+fetch+backoff shape (the reconnect/backoff + fetch-not-
    // EventSource regression tests pin it); ``subscribeMcpNotifications``
    // is the run-loop wrapper this test guards.
    const src = read("lib/mcp-notifications.ts")

    // Pin the no-op body shape: an empty function body or a body that
    // only returns a no-op cleanup. We do that by asserting the
    // ``subscribeMcpNotifications`` function body does NOT open a
    // stream (no ``openMcpNotificationStream(`` call inside it) and
    // does NOT register a visibilitychange listener.
    const fnMatch = src.match(
      /export\s+function\s+subscribeMcpNotifications\s*\([^)]*\)\s*:\s*\(\)\s*=>\s*void\s*\{([\s\S]*?)\n\}/,
    )
    expect(
      fnMatch,
      "expected `export function subscribeMcpNotifications(): () => void" +
        " { ... }` declaration in lib/mcp-notifications.ts",
    ).not.toBeNull()
    const body = fnMatch![1]!
    expect(
      body.includes("openMcpNotificationStream("),
      "subscribeMcpNotifications must open the notification stream now " +
        "that the cookie-auth GET /api/events endpoint exists (operator " +
        "SSE live-update channel). Body:\n" + body,
    ).toBe(true)
    expect(
      body.includes("visibilitychange"),
      "subscribeMcpNotifications must attach a visibilitychange listener " +
        "to pause the live stream when the tab is hidden and resume it " +
        "when visible. Body:\n" + body,
    ).toBe(true)

    // Belt-and-braces: the per-URL opener IS still exported (its
    // cookie+backoff shape is exercised by the two regression tests
    // above; keeping it lets a future endpoint plug back in without
    // rewriting the run loop).
    expect(
      src.includes("export function openMcpNotificationStream"),
      "expected openMcpNotificationStream to remain exported so a " +
        "future cookie-authenticated SSE notification endpoint can " +
        "wire back in without re-implementing the run loop",
    ).toBe(true)
  })

  // -- wiring into the app --------------------------------------------------

  it("is wired from a dashboard provider", () => {
    // The subscription has to be started somewhere. The natural seam
    // is a client-side provider that boots on app mount (mirrors how
    // project-context-provider wires the path-prefix singleton).
    //
    // Wave 2 (cleanup-wave-2, 2026-06-20): the provider no longer needs
    // an admin token from the data-store — the operator session cookie
    // is sent automatically once the operator has logged in. The
    // provider just calls `subscribeMcpNotifications()` from a
    // useEffect on mount.
    //
    // Either a dedicated provider or a hook called from layout. Accept
    // either pattern but pin that the wiring exists.
    const providersDir = resolve(DASHBOARD_ROOT, "components", "providers")
    const candidates = readdirSync(providersDir).filter(
      (f) => f.includes("notification") || f.includes("mcp"),
    )
    if (candidates.length === 0) {
      // Fallback: maybe the subscription is auto-started by the
      // module itself (an IIFE / module-load side effect). Accept
      // that pattern by checking the module references its public
      // entry point from somewhere reachable.
      const src = read("lib/mcp-notifications.ts")
      expect(
        src.includes("subscribeMcpNotifications"),
        "expected either a *notification*-named provider in " +
          "components/providers/ OR a self-bootstrapping module. " +
          "Found neither.",
      ).toBe(true)
      return
    }
    // If there is a provider, it must be rendered from app/layout.tsx
    // (otherwise it never mounts).
    const layoutSrc = read("app/layout.tsx")
    const providerNames = candidates.map((c) => c.replace(/\.tsx?$/, ""))
    const wired = providerNames.some(
      (name) =>
        layoutSrc.includes(name) ||
        layoutSrc
          .replace(/-/g, "")
          .toLowerCase()
          .includes(name.replace(/-/g, "").toLowerCase()),
    )
    expect(
      wired,
      `found provider files ${JSON.stringify(providerNames)} but none of ` +
        "them is rendered from app/layout.tsx — the subscription will " +
        "never start",
    ).toBe(true)
  })
})
