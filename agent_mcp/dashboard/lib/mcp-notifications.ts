"use client"

/**
 * Dashboard subscription to the operator live-update SSE stream.
 *
 * Background
 * ----------
 * PR #79 (Candidate A, 2026-06-02) wired session_registry into the
 * GET /mcp transport so `fanout_to_agent()` delivers `notifications/...`
 * JSON-RPC envelopes onto subscribed AGENT sessions. The dashboard,
 * however, authenticates with an operator session cookie — not a
 * per-agent bearer — and the MCP StreamableHTTP GET stream rejects a
 * cookie-only open with 405 (it derives `agent_id` from a bearer the
 * cookie can't carry, and needs an `Mcp-Session-Id` from an
 * `initialize` handshake the dashboard never does). So this client
 * never actually connected against `/mcp`.
 *
 * Fix: a dedicated cookie-authenticated operator SSE endpoint,
 * `GET /agent-mcp/api/<name>/events`, backed by an in-process
 * operator-events hub (`features/operator_events.py`). The same
 * mutation choke point that fans `resources/updated` out to agent
 * sessions also publishes onto that hub, so this client receives the
 * identical payload — the JSON-RPC → store-invalidation glue below is
 * unchanged. When a notification arrives, the relevant zustand cache
 * slice is invalidated — admin-created prompts visible in other tabs
 * within seconds rather than waiting the full 60s data-store poll tick.
 *
 * URL plumbing
 * ------------
 * The dashboard mounts at `/agent-mcp/app/<name>/...` and the REST API
 * at `/agent-mcp/api/<name>` (PR-B renamed both from /__dashboard/ and
 * /__api/ respectively). The operator events channel lives UNDER the
 * REST root at `/agent-mcp/api/<name>/events` — the router's `/api/...`
 * proxy streams response bodies chunk-by-chunk, so the SSE frames flow
 * through, and the operator session cookie carries the auth. See
 * `eventsUrl` in lib/urls.ts.
 *
 * Auth
 * ----
 * `EventSource` cannot send custom headers and cannot carry cookies
 * on cross-origin requests reliably; we use `fetch()` + a
 * ReadableStream reader instead and parse SSE framing manually.
 *
 * Wave 2 (cleanup-wave-2) migrated this client off bearer auth and
 * onto cookie auth. The fetch opts into `credentials: "include"` so
 * the `agent_mcp_session` cookie (set by /agent-mcp/login) is sent
 * with the request. The router's `backend_mcp_handler` then validates
 * the cookie + project membership and injects the project's admin
 * token upstream so the backend `AuthHeaderMiddleware` (still
 * bearer-only) sees a valid bearer. The dashboard never holds an
 * admin token in JS memory anymore — the cookie is HttpOnly and
 * never reaches the React tree.
 *
 * Resilience
 * ----------
 * - On disconnect: reconnect with exponential backoff capped at 30s.
 * - On `visibilitychange` → hidden: close the stream (the browser
 *   throttles background timers anyway and a parked SSE socket is
 *   wasteful). On visible again: reopen immediately, restart backoff
 *   from 1s.
 *
 * Notification dispatch
 * ---------------------
 * The backend currently emits three notification methods (see
 * `tests/test_session_registry_transport.py` + the resources/prompts/
 * tools subsystems):
 *
 * | method                                | dashboard reaction              |
 * |---------------------------------------|---------------------------------|
 * | notifications/resources/updated       | refreshData() → messages list,  |
 * |   (uri = agent-mcp://inbox/<id> OR    |   counters reflect the new      |
 * |    agent-mcp://status/<id>)           |   inbox row / status change     |
 * | notifications/prompts/list_changed    | notifyPromptsListChanged() →    |
 * |                                       |   prompt-book reflects admin    |
 * |                                       |   create/update/delete          |
 * | notifications/tools/list_changed      | refreshData() — covers any UI   |
 * |                                       |   bound to the tool catalogue   |
 */

import { useDataStore, notifyPromptsListChanged } from "./stores/data-store"
import { projectContext } from "./project-context"
import { eventsUrl } from "./urls"

// -- URL construction -----------------------------------------------------

/**
 * Build the operator live-update SSE endpoint URL for the active
 * project.
 *
 * Path-prefixed deployments: `/agent-mcp/api/<projectName>/events`
 * (the cookie-authenticated operator events channel under the REST
 * proxy root — see `eventsUrl` in lib/urls.ts).
 *
 * Standalone (single-tenant) deployments: relative `/api/events` on
 * the same origin the dashboard is served from. The
 * `setServer(host, port)` path uses `http://host:port/api` as baseUrl —
 * derive the events URL from that origin.
 */
export function eventsUrlForProject(): string {
  if (projectContext.projectName) {
    return eventsUrl(projectContext.projectName)
  }
  // Standalone: prefer the apiClient baseUrl's origin if it's an
  // absolute URL (the `setServer(host, port)` path). Otherwise fall
  // back to a same-origin `/api/events`.
  const base = projectContext.baseUrl
  if (base.startsWith("http://") || base.startsWith("https://")) {
    try {
      const u = new URL(base)
      return u.origin + "/api/events"
    } catch {
      /* fall through */
    }
  }
  return "/api/events"
}

// -- SSE frame parser -----------------------------------------------------

/**
 * Minimal SSE frame parser. Yields parsed JSON-RPC envelopes from a
 * `data:` field stream. Tolerates the optional `event:`/`id:`/`:` lines
 * (comments, named events) by ignoring them — the MCP transport only
 * uses `data:` frames for JSON-RPC payloads.
 */
function* parseSseFrames(buffer: string): Generator<string, void, void> {
  // SSE frames are separated by a blank line. Split, keep the last
  // (possibly partial) chunk for the next call.
  const frames = buffer.split(/\r?\n\r?\n/)
  for (let i = 0; i < frames.length - 1; i++) {
    yield frames[i]
  }
}

function dataFromFrame(frame: string): string | null {
  const lines = frame.split(/\r?\n/)
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart())
    }
  }
  if (dataLines.length === 0) return null
  return dataLines.join("\n")
}

// -- notification dispatch -----------------------------------------------

interface JsonRpcNotification {
  jsonrpc?: string
  method?: string
  params?: { uri?: string; [k: string]: unknown }
}

/**
 * Route a parsed JSON-RPC notification envelope to the right store
 * invalidation. Exported for unit-test reach (a future jsdom suite
 * would call this directly with synthetic frames).
 */
// Coalesce bursts of resource-change notifications into a single
// refetch. The backend now fires one `resources/updated` per mutation
// (from the action-log choke point in agent_actions_db.py), so an active
// project emits many in a tight window; debouncing collapses them into
// one all-data refetch. The short delay also lets the caller's DB commit
// land before the refetch reads — the backend notification fires
// pre-commit as a "refetch soon" hint — avoiding a read-before-commit
// race.
const DASHBOARD_REFRESH_DEBOUNCE_MS = 300
let _dashboardRefreshTimer: ReturnType<typeof setTimeout> | null = null
function scheduleDashboardRefresh(): void {
  if (_dashboardRefreshTimer !== null) clearTimeout(_dashboardRefreshTimer)
  _dashboardRefreshTimer = setTimeout(() => {
    _dashboardRefreshTimer = null
    void useDataStore.getState().refreshData()
  }, DASHBOARD_REFRESH_DEBOUNCE_MS)
}

/**
 * Drop a debounced refetch that hasn't fired yet.
 *
 * Called from a subscription's `stop()`: the debounce exists to
 * coalesce stream traffic, so once the stream is gone the pending tick
 * has nothing left to reconcile. Without this it survived teardown and
 * drove a `refreshData()` 300ms after unmount / navigate-away / tab-
 * hide — a stray request against a store nobody is rendering, and a
 * live timer in a finished vitest worker. A later reconnect re-arms it
 * via the catch-up dispatch, so nothing is lost.
 */
function cancelScheduledDashboardRefresh(): void {
  if (_dashboardRefreshTimer !== null) {
    clearTimeout(_dashboardRefreshTimer)
    _dashboardRefreshTimer = null
  }
}

export function dispatchNotification(payload: JsonRpcNotification): void {
  const method = payload?.method
  if (!method || typeof method !== "string") return

  if (method === "notifications/prompts/list_changed") {
    // Promptbook delta — invalidate cached catalogue, force refetch.
    notifyPromptsListChanged()
    return
  }

  if (method === "notifications/resources/updated") {
    // Resource churn — inbox/<agent_id> for new messages,
    // status/<agent_id> for ambient counters. The data-store carries
    // both messages + agent state in its all-data envelope, so a
    // single refresh covers both. Forced (skips the 30s freshness
    // window) so a tight succession of notifications surfaces each
    // change rather than coalescing into one invisible no-op.
    const uri = typeof payload.params?.uri === "string" ? payload.params.uri : ""
    // Light filtering: known agent-mcp:// URIs trigger a refresh; an
    // unknown scheme still triggers (defensive: better to over-refresh
    // than miss) but only logs a debug line for traceability.
    if (!uri.startsWith("agent-mcp://")) {
      console.debug("[mcp-notifications] unknown resource uri:", uri)
    }
    scheduleDashboardRefresh()
    // Phase 3.5a — also fan out a window event so the cross-project
    // overview store can refresh without importing the per-project
    // data store (the overview route doesn't load the data-store
    // module at all). The event is best-effort: missing window means
    // SSR, in which case there's no listener to call.
    if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("mcp:resources-updated", { detail: { uri } }),
      )
    }
    return
  }

  if (method === "notifications/tools/list_changed") {
    // No dedicated tool-catalogue cache yet; the data-store envelope
    // carries everything the dashboard renders, so a refresh is the
    // catch-all. Documented here so a future tool-catalogue slice can
    // hook in by replacing this call.
    scheduleDashboardRefresh()
    return
  }

  // Unknown notification method — surface for debugging without
  // crashing the stream loop.
  console.debug("[mcp-notifications] unhandled method:", method)
}

// -- subscription lifecycle ----------------------------------------------

interface SubscriptionHandle {
  stop: () => void
}

const RECONNECT_BASE_DELAY_MS = 1000
const RECONNECT_MAX_DELAY_MS = 30000

/**
 * Start an operator notification subscription against
 * `/agent-mcp/api/<name>/events` using the operator session cookie.
 * Returns a handle whose `stop()` aborts the in-flight stream and
 * prevents further reconnects.
 *
 * The loop schedules reconnects on disconnect (transport error or
 * stream end) with exponential backoff: `min(30000, 1000 * 2 **
 * attempt)`. The visibility handler closes/reopens on tab background
 * transitions — see `subscribeMcpNotifications` for that wiring.
 */
export function openMcpNotificationStream(
  options: { url?: string } = {}
): SubscriptionHandle {
  const url = options.url ?? eventsUrlForProject()
  let stopped = false
  let attempt = 0
  let abortCtrl: AbortController | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  const scheduleReconnect = (): void => {
    if (stopped) return
    const delay = Math.min(
      RECONNECT_MAX_DELAY_MS,
      RECONNECT_BASE_DELAY_MS * 2 ** attempt
    )
    attempt += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      void run()
    }, delay)
  }

  const run = async (): Promise<void> => {
    if (stopped) return
    abortCtrl = new AbortController()
    try {
      const res = await fetch(url, {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
        },
        signal: abortCtrl.signal,
        cache: "no-store",
        // Wave 2 (cleanup-wave-2): cookie auth. The
        // ``agent_mcp_session`` cookie set by /agent-mcp/login is
        // sent automatically; the router's backend_mcp_handler
        // resolves it to the project's admin token and injects the
        // bearer upstream so the backend's AuthHeaderMiddleware
        // (still bearer-only) accepts the request.
        credentials: "include",
      })
      if (!res.ok) {
        console.debug(
          `[mcp-notifications] subscribe failed: ${res.status} ${res.statusText}`
        )
        scheduleReconnect()
        return
      }
      const body = res.body
      if (!body) {
        console.debug("[mcp-notifications] response has no body")
        scheduleReconnect()
        return
      }

      // Successful connection — reset backoff so a later drop starts
      // from 1s again.
      attempt = 0

      // Catch-up on (re)connect. The operator-events hub is
      // fire-and-forget: any mutation that happened while this stream
      // was down (a transport drop, a router read-timeout, a
      // tab-hidden→visible reopen, a backend restart) published to zero
      // subscribers and is gone — there is no replay buffer. So on every
      // successful connect we synthesize a resources/updated to force a
      // full refetch, which reconciles whatever changed during the gap.
      // The 300ms debounce coalesces this with the initial page-load
      // fetch; the window event also nudges the per-page pollers
      // (Messages/Tasks) the same way a real notification does.
      dispatchNotification({
        method: "notifications/resources/updated",
        params: { uri: "agent-mcp://reconnect" },
      })

      const reader = body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (!stopped) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // Drain complete frames; keep the trailing partial in buffer.
        const lastBoundary = buffer.lastIndexOf("\n\n")
        if (lastBoundary === -1) continue
        const complete = buffer.slice(0, lastBoundary + 2)
        buffer = buffer.slice(lastBoundary + 2)
        for (const frame of parseSseFrames(complete + "\n\n")) {
          const data = dataFromFrame(frame)
          if (!data) continue
          try {
            const payload = JSON.parse(data) as JsonRpcNotification
            dispatchNotification(payload)
          } catch (err) {
            console.debug(
              "[mcp-notifications] failed to parse SSE data frame:",
              err
            )
          }
        }
      }
    } catch (err) {
      // AbortError is the expected case for stop() / visibility-hide;
      // anything else is a real transport error worth a debug line.
      if (
        err instanceof DOMException &&
        err.name === "AbortError"
      ) {
        // No reconnect on intentional abort.
        return
      }
      console.debug("[mcp-notifications] stream error:", err)
    } finally {
      abortCtrl = null
    }
    scheduleReconnect()
  }

  void run()

  return {
    stop: () => {
      stopped = true
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
      cancelScheduledDashboardRefresh()
      if (abortCtrl) abortCtrl.abort()
    },
  }
}

/**
 * Higher-level wiring entry point used by ``McpNotificationsProvider``.
 *
 * Opens the operator live-update SSE stream against
 * ``/agent-mcp/api/<name>/events`` (cookie-authenticated) and manages
 * its lifecycle. Returns a cleanup that stops the stream and detaches
 * the visibility listener.
 *
 * History: this was a no-op between verify-all-v8 (2026-06-27) and the
 * introduction of the dedicated operator events endpoint. Before the
 * no-op, the client subscribed to ``GET /agent-mcp/mcp/<project>`` with
 * cookie-only auth, which the router rejects with 405 (that GET stream
 * derives ``agent_id`` from a per-agent bearer the cookie can't carry),
 * generating 60+ ``=> 405`` lines within seconds of any project page
 * load. The fix was a purpose-built cookie-authenticated endpoint
 * (``features/operator_events.py`` + ``GET /api/events``); this function
 * now opens a stream against it — no more 405 spam, and live updates
 * actually reach the browser.
 *
 * Lifecycle:
 *   - Opens immediately via ``openMcpNotificationStream`` (which owns
 *     the reconnect/backoff loop).
 *   - On ``visibilitychange`` → hidden: stops the stream (the browser
 *     throttles background timers anyway and a parked SSE socket is
 *     wasteful). On visible again: reopens, restarting backoff from 1s.
 */
export function subscribeMcpNotifications(): () => void {
  // The cross-project overview (router-served, no project selected) has
  // no per-project `/api/<project>/events` stream to subscribe to — the
  // events feed is per-project. Subscribing there resolves to a bare
  // `/api/events`, which 404s in a reconnect loop. Skip it; the overview
  // refreshes via `/api/router/overview`. Per-project pages subscribe
  // normally. (Standalone single-tenant is NOT isOverview, so it still
  // subscribes to its `/api/events`.)
  if (projectContext.isOverview) {
    return () => {}
  }

  let handle: SubscriptionHandle | null = openMcpNotificationStream()

  // Guard for SSR / non-DOM (vitest node) environments — there's no
  // ``document`` to attach a visibility listener to. The stream itself
  // is still opened above (harmless in tests; the fetch is stubbed).
  const hasDocument = typeof document !== "undefined"

  const onVisibility = (): void => {
    if (document.visibilityState === "hidden") {
      if (handle) {
        handle.stop()
        handle = null
      }
    } else if (handle === null) {
      handle = openMcpNotificationStream()
    }
  }

  if (hasDocument) {
    document.addEventListener("visibilitychange", onVisibility)
  }

  return () => {
    if (hasDocument) {
      document.removeEventListener("visibilitychange", onVisibility)
    }
    if (handle) {
      handle.stop()
      handle = null
    }
  }
}
