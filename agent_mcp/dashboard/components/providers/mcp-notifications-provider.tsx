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

export function McpNotificationsProvider({
  children,
}: {
  children: React.ReactNode
}) {
  useEffect(() => {
    const unsubscribe = subscribeMcpNotifications()
    // Wave 6 keystone increment 1: the 60s freshness poll that used to
    // be started here (`startDataStoreAutoRefresh`) is gone — the
    // `/all-data` TanStack Query owns its own `refetchInterval`, gated
    // on `sseHealthy` (PF-3) so it only polls while this stream is down.
    return () => {
      unsubscribe()
    }
  }, [])

  return <>{children}</>
}
