"use client"

/**
 * TanStack Query hook for the schedules list (`GET /schedules`).
 *
 * Migrates schedules-dashboard.tsx off its hand-rolled `useState` +
 * one-shot `apiClient.getSchedules()` `useEffect` (no polling, no SSE
 * invalidation — "Next fire" froze until a manual page refresh). Follows
 * the `useTasksQuery` pattern: one query per `['schedules', project]`,
 * invalidated from the debounced SSE dispatcher
 * (`invalidateSchedules()` in `lib/query-client.ts`, called from
 * `scheduleDashboardRefresh()` in `lib/mcp-notifications.ts`), with the
 * same PF-3 poll-gating on SSE health as a fallback.
 *
 * No server-side filters exist for `GET /schedules` (contrast
 * `useTasksQuery`'s `TaskFilters` param) — the agent/status filters on
 * the Schedules page are client-side over the full fetched set, so the
 * query key carries only the project.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { apiClient, type Schedule } from "../api"
import { projectContext } from "../project-context"
import { schedulesQueryKey } from "../query-client"
import { useServerStore } from "../stores/server-store"
import { useSseHealthy } from "../stores/data-store"

/**
 * The schedules-list background poll interval (ms). Safety net BEHIND
 * the live-update SSE stream — suppressed while SSE is healthy (PF-3).
 * Matches `useTasksQuery`'s `AUTO_REFRESH_INTERVAL_MS`.
 */
const AUTO_REFRESH_INTERVAL_MS = 60_000

/** True while an active, connected server is selected. */
function useIsConnected(): boolean {
  return useServerStore((s) => {
    const active = s.servers.find((x) => x.id === s.activeServerId)
    return !!s.activeServerId && active?.status === "connected"
  })
}

/**
 * The schedules-list query.
 *
 * Gating mirrors `useTasksQuery`: `enabled` on server connection;
 * `refetchInterval` suppressed while SSE is healthy, else a 60s
 * fallback poll; no background-tab polling.
 */
export function useSchedulesQuery(): UseQueryResult<Schedule[]> {
  const enabled = useIsConnected()
  const sseHealthy = useSseHealthy()
  return useQuery({
    queryKey: schedulesQueryKey(projectContext.projectName),
    queryFn: async () => {
      try {
        return await apiClient.getSchedules()
      } catch (err) {
        if (err instanceof Error && err.message === "NO_SERVER_CONNECTED") {
          return []
        }
        throw err
      }
    },
    enabled,
    refetchInterval: sseHealthy ? false : AUTO_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })
}
