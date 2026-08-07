"use client"

/**
 * Operator notification subscription provider.
 *
 * Role
 * ----
 * Boots the dashboard's subscription to the operator live-update SSE
 * stream (``GET /agent-mcp/api/<name>/events``). When a notification
 * arrives, the relevant zustand cache slice is invalidated — admin-
 * created prompts visible in other tabs within seconds, messages/tasks
 * lists updating without waiting for the 60s poll tick, etc.
 *
 * History: ``subscribeMcpNotifications`` was a no-op between the
 * verify-all-v8 405-spam fix (2026-06-27) and the introduction of the
 * dedicated cookie-authenticated operator events endpoint
 * (``features/operator_events.py`` + ``GET /api/events``). Before the
 * no-op, subscribing to the agent-scoped ``GET /mcp`` with cookie-only
 * auth produced continuous 405s. The provider was intentionally kept
 * mounted so re-enabling was a body-only edit in
 * ``lib/mcp-notifications.ts`` — which is exactly what happened.
 *
 * The provider itself never opens a stream directly — it only calls
 * ``subscribeMcpNotifications`` (which owns the endpoint choice +
 * reconnect/visibility lifecycle). ``tests/mcp-notifications-no-poll``
 * pins that boundary.
 *
 * Renders no UI — pure side-effect wiring inside a useEffect, so the
 * component slots cleanly into the layout tree as a sibling of
 * `<ProjectContextProvider>` without affecting children.
 */

import { useEffect } from "react"
import { subscribeMcpNotifications } from "@/lib/mcp-notifications"
import { startDataStoreAutoRefresh } from "@/lib/stores/data-store"

export function McpNotificationsProvider({
  children,
}: {
  children: React.ReactNode
}) {
  useEffect(() => {
    const unsubscribe = subscribeMcpNotifications()
    // The data-store's 60s freshness poll is the safety net BEHIND this
    // stream, so its lifecycle belongs next to the stream's rather than
    // firing as an import-time side effect of `lib/stores/data-store`
    // (where it was unstoppable and re-armed on every module
    // re-evaluation). See `startDataStoreAutoRefresh`.
    const stopAutoRefresh = startDataStoreAutoRefresh()
    return () => {
      unsubscribe()
      stopAutoRefresh()
    }
  }, [])

  return <>{children}</>
}
