"use client"

/**
 * Dashboard subscription to the MCP `notifications/...` SSE stream.
 *
 * Background
 * ----------
 * PR #79 (Candidate A, 2026-06-02) wired session_registry into the
 * GET /mcp transport: opening a stream registers a row + queue, and
 * `fanout_to_agent()` now actually delivers `notifications/...`
 * JSON-RPC envelopes onto subscribed sessions. Before that, the
 * fan-out walked an empty `_runtime_queues` and every notification
 * dropped at the wire.
 *
 * Candidate E (this module) wires the dashboard onto that stream.
 * When a notification arrives, the relevant zustand cache slice is
 * invalidated — admin-created prompts visible in other tabs within
 * seconds rather than waiting the full 60s of the data-store poll
 * tick.
 *
 * URL plumbing
 * ------------
 * The dashboard mounts at `/agent-mcp/app/<name>/...` and the REST
 * API at `/agent-mcp/api/<name>` (PR-B renamed both from /__dashboard/
 * and /__api/ respectively). The MCP Streamable HTTP endpoint for the
 * same project is at `/agent-mcp/<name>/mcp` — NOT the /api/ prefix;
 * that's the REST proxy. The dashboard's
 * `apiClient.createEventSource('/mcp')` would concatenate '/mcp'
 * onto baseUrl and hit the REST proxy prefix (a 404 — the router
 * doesn't expose /mcp under the REST root). We therefore build the
 * MCP URL separately, rooted at /agent-mcp/<name>/mcp directly.
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
import { mcpUrl } from "./urls"

// -- URL construction -----------------------------------------------------

/**
 * Build the MCP Streamable HTTP endpoint URL for the active project.
 *
 * Path-prefixed deployments: `/agent-mcp/<projectName>/mcp` (the
 * router-mounted wrapped backend; NOT the `__api` REST prefix).
 *
 * Standalone (single-tenant) deployments: relative `/mcp` on the same
 * origin the dashboard is served from. The `setServer(host, port)`
 * path uses `http://host:port/api` as baseUrl — derive the MCP URL by
 * swapping `/api` → `/mcp` on the same origin.
 */
export function mcpUrlForProject(): string {
  if (projectContext.projectName) {
    // PR-B: route through the shared URL helper. PR-D will move the
    // MCP path to /agent-mcp/mcp/<name>; that change becomes a
    // one-line edit in lib/urls.ts.
    return mcpUrl(projectContext.projectName)
  }
  // Standalone: prefer the apiClient baseUrl's origin if it's an
  // absolute URL (the `setServer(host, port)` path). Otherwise fall
  // back to a same-origin `/mcp`. We use `URL(...).origin` rather
  // than interpolating baseUrl directly (which would carry the `/api`
  // suffix and resolve to `/api/mcp`, a 404).
  const base = projectContext.baseUrl
  if (base.startsWith("http://") || base.startsWith("https://")) {
    try {
      const u = new URL(base)
      const origin = u.origin
      return origin + "/mcp"
    } catch {
      /* fall through */
    }
  }
  return "/mcp"
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
 * Start an MCP notification subscription against `/agent-mcp/mcp/<name>`
 * using the operator session cookie (Wave 2, cleanup-wave-2). Returns
 * a handle whose `stop()` aborts the in-flight stream and prevents
 * further reconnects.
 *
 * The loop schedules reconnects on disconnect (transport error or
 * stream end) with exponential backoff: `min(30000, 1000 * 2 **
 * attempt)`. The visibility handler closes/reopens on tab background
 * transitions — see `subscribeMcpNotifications` for that wiring.
 */
export function openMcpNotificationStream(
  options: { url?: string } = {}
): SubscriptionHandle {
  const url = options.url ?? mcpUrlForProject()
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
      if (abortCtrl) abortCtrl.abort()
    },
  }
}

/**
 * Higher-level wiring entry point used by ``McpNotificationsProvider``.
 *
 * No-op as of verify-all-v8 (2026-06-27). The router's
 * ``backend_mcp_handler`` rejects ``GET /agent-mcp/mcp/<project>`` from
 * cookie-only callers with 405 (PR #220, F015 fix) because the
 * backend's ``_handle_get`` derives ``agent_id`` from a per-agent
 * bearer that the cookie path can't carry. Wave 2 (cleanup-wave-2)
 * removed the router-side cookie→admin-bearer translation that
 * previously let this subscription proceed, so every reconnect
 * attempt now produces a 405 in the user's browser network tab.
 *
 * Before this no-op was introduced, the dashboard generated 60+ ``GET
 * /agent-mcp/mcp/<project> => 405`` lines within seconds of any
 * project page load (user reproduction on
 * https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/app/
 * washing-brothers/?page=memories surfaced this as "login errors").
 * PR #220's bg-agent report explicitly flagged the subscription as
 * "silently failing" — turning it into a no-op closes the spam loop.
 *
 * The dispatch glue (``dispatchNotification``) and the per-URL stream
 * opener (``openMcpNotificationStream``) remain exported so a future
 * cookie-authenticated SSE notification endpoint — e.g.
 * ``/agent-mcp/api/<name>/notifications`` accepting the operator
 * session cookie — can wire back in without rewriting the JSON-RPC →
 * store invalidation glue. When that endpoint exists, this function
 * should resume opening a stream against it (with the
 * visibility/reconnect lifecycle that lived here previously) and the
 * ``tests/mcp-notifications-no-poll.test.ts`` contract should be
 * updated to assert the new endpoint shape.
 *
 * Returns a no-op cleanup function so the calling React effect's
 * ``useEffect(() => subscribe(), [])`` shape stays identical and a
 * future re-wire is a one-function-body change.
 */
export function subscribeMcpNotifications(): () => void {
  // Intentionally empty — see the function comment above. No fetch
  // fires; no visibility listener attaches; the returned cleanup is a
  // no-op. The shape of the function (parameterless, returns a void
  // thunk) is preserved so re-enabling the stream against a real
  // cookie-authenticated endpoint is a body-only edit.
  return () => {
    /* no-op */
  }
}
