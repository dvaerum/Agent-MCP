"use client"

/**
 * MCP notification subscription provider.
 *
 * Historical role
 * ---------------
 * Boots the dashboard's subscription to the GET /mcp SSE stream
 * (Candidate E, architecture review 2026-06-02). When notifications
 * arrive, the relevant zustand cache slice is invalidated — admin-
 * created prompts visible in other tabs within seconds, messages list
 * updates without waiting for the 60s data-store poll, etc.
 *
 * Current state — no-op (verify-all-v8, 2026-06-27)
 * --------------------------------------------------
 * ``subscribeMcpNotifications`` is a no-op as of the 405-spam fix:
 * the only SSE notification endpoint on the backend (``GET /mcp``)
 * requires a per-agent bearer that the dashboard cookie path can't
 * carry (Wave 2 stripped the cookie→admin-bearer translation), and
 * PR #220 closed the resulting 500 with a 405. Mounting this provider
 * therefore no longer fires any HTTP traffic.
 *
 * The provider is intentionally kept (rather than removed from
 * ``app/layout.tsx``) so re-enabling notifications against a future
 * cookie-authenticated endpoint becomes a body-only edit in
 * ``lib/mcp-notifications.ts`` — no layout churn, no additional
 * regression-test refactor.
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
