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
 * Boot ordering. The subscription needs:
 *   1. An admin bearer token, which lives on
 *      `useDataStore.getState().data.admin_token` after the first
 *      `fetchAllData()` completes.
 *   2. A path-prefixed deployment (project name resolvable from
 *      `window.location.pathname`). Standalone (single-tenant) builds
 *      still benefit — `mcpUrlForProject()` falls back to a same-
 *      origin `/mcp`.
 *
 * This provider mounts once at app boot, subscribes to the data-store
 * to learn the admin token, and starts the MCP subscription as soon
 * as a token first becomes available. The `subscribeMcpNotifications`
 * helper owns the visibility/reconnect lifecycle from there.
 *
 * Renders no UI — pure side-effect wiring inside a useEffect, so the
 * component slots cleanly into the layout tree as a sibling of
 * `<ProjectContextProvider>` without affecting children.
 */

import { useEffect } from "react"
import { useDataStore } from "@/lib/stores/data-store"
import { subscribeMcpNotifications } from "@/lib/mcp-notifications"

export function McpNotificationsProvider({
  children,
}: {
  children: React.ReactNode
}) {
  useEffect(() => {
    // Eager check: the data-store might already have an admin_token
    // (e.g. a fast-arriving fetchAllData from another component, or a
    // hot-reload that preserved zustand state). If so, start
    // immediately and skip the subscribe-once dance.
    const initialToken = useDataStore.getState().data?.admin_token
    let unsubscribe: (() => void) | null = null
    if (initialToken) {
      unsubscribe = subscribeMcpNotifications(initialToken)
    }

    // Otherwise, wait for the first non-empty admin_token to appear,
    // then start. The data-store auto-fetches on mount (the dashboard
    // overview triggers it) so this fires within seconds of page load.
    const unsubFromStore = useDataStore.subscribe(state => {
      const token = state.data?.admin_token
      if (token && !unsubscribe) {
        unsubscribe = subscribeMcpNotifications(token)
      }
    })

    return () => {
      unsubFromStore()
      if (unsubscribe) unsubscribe()
    }
  }, [])

  return <>{children}</>
}
