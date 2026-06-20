"use client"

/**
 * MCP notification subscription provider.
 *
 * Boots the dashboard's subscription to the GET /mcp SSE stream
 * (Candidate E, architecture review 2026-06-02). When notifications
 * arrive, the relevant zustand cache slice is invalidated — admin-
 * created prompts visible in other tabs within seconds, messages list
 * updates without waiting for the 60s data-store poll, etc.
 *
 * Boot ordering. Wave 2 (cleanup-wave-2) replaced the boot-time
 * admin-token wait with cookie auth: the subscription only needs:
 *
 *   1. The operator session cookie (set by /agent-mcp/login). The
 *      browser attaches it automatically once the operator has logged
 *      in; this provider mounts under the auth-required route tree so
 *      that's already true at mount time.
 *   2. A path-prefixed deployment (project name resolvable from
 *      `window.location.pathname`). Standalone (single-tenant) builds
 *      still benefit — `mcpUrlForProject()` falls back to a same-
 *      origin `/mcp`.
 *
 * This provider mounts once at app boot and immediately starts the
 * subscription — no store subscribe-once dance is needed anymore.
 * The `subscribeMcpNotifications` helper owns the visibility/reconnect
 * lifecycle from there.
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
    return () => {
      unsubscribe()
    }
  }, [])

  return <>{children}</>
}
