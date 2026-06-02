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
 * The dashboard mounts at `/agent-mcp/__dashboard/<name>/...` and the
 * REST API at `/agent-mcp/__api/<name>` (Candidate C path-prefix
 * adapter, lib/project-context.ts). The MCP Streamable HTTP endpoint
 * for the same project is at `/agent-mcp/<name>/mcp` — NOT the
 * `__api` prefix; that's the REST proxy. The dashboard's
 * `apiClient.createEventSource('/mcp')` would concatenate '/mcp'
 * onto baseUrl and hit the REST proxy prefix (a 404 — the router
 * doesn't expose /mcp under the REST root). We therefore build the
 * MCP URL separately, rooted at /agent-mcp/<name>/mcp directly.
 *
 * Auth
 * ----
 * `EventSource` cannot send custom headers, so it can't authenticate
 * with `Authorization: Bearer <token>` (the only auth scheme /mcp's
 * middleware accepts). We use `fetch()` + a ReadableStream reader
 * instead and parse SSE framing manually.
 *
 * Bearer token
 * ------------
 * The dashboard operates as admin (ADR-0003). The admin token is
 * exposed on `useDataStore.getState().data.admin_token` after the
 * first `fetchAllData()` completes. The subscription waits for that
 * via `useDataStore.subscribe(...)` and starts when the token first
 * appears.
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
    return `/agent-mcp/${projectContext.projectName}/mcp`
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
    void useDataStore.getState().refreshData()
    return
  }

  if (method === "notifications/tools/list_changed") {
    // No dedicated tool-catalogue cache yet; the data-store envelope
    // carries everything the dashboard renders, so a refresh is the
    // catch-all. Documented here so a future tool-catalogue slice can
    // hook in by replacing this call.
    void useDataStore.getState().refreshData()
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
 * Start an MCP notification subscription against `/agent-mcp/<name>/mcp`
 * using the supplied bearer. Returns a handle whose `stop()` aborts
 * the in-flight stream and prevents further reconnects.
 *
 * The loop schedules reconnects on disconnect (transport error or
 * stream end) with exponential backoff: `min(30000, 1000 * 2 **
 * attempt)`. The visibility handler closes/reopens on tab background
 * transitions — see `subscribeMcpNotifications` for that wiring.
 */
export function openMcpNotificationStream(
  bearer: string,
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
          Authorization: `Bearer ${bearer}`,
          Accept: "text/event-stream",
        },
        signal: abortCtrl.signal,
        cache: "no-store",
        // The MCP endpoint lives on the same origin; default cors mode
        // is fine. Don't include cookies — bearer auth is the only
        // accepted scheme.
        credentials: "omit",
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
 * Higher-level wiring: opens a subscription when `bearer` is non-empty,
 * closes it on document.hidden, reopens on document.visible, and tears
 * down on the returned `unsubscribe()`.
 *
 * Returns a function that fully unsubscribes (stops the stream + drops
 * the visibility listener). Intended to be called from a React effect's
 * cleanup.
 */
export function subscribeMcpNotifications(bearer: string): () => void {
  if (!bearer) return () => {}

  let handle: SubscriptionHandle | null = openMcpNotificationStream(bearer)

  const onVisibility = (): void => {
    if (typeof document === "undefined") return
    if (document.hidden) {
      if (handle) {
        handle.stop()
        handle = null
      }
    } else {
      if (!handle) {
        handle = openMcpNotificationStream(bearer)
      }
    }
  }

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibility)
  }

  return () => {
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibility)
    }
    if (handle) {
      handle.stop()
      handle = null
    }
  }
}
