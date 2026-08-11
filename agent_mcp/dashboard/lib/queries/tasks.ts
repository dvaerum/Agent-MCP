"use client"

/**
 * TanStack Query hook for the tasks list (`GET /tasks`).
 *
 * W6-followup increment F2 (2026-08-11): tasks-dashboard used to fetch
 * via the hand-rolled `usePagedQuery` hook (a bespoke
 * data+loading+error+refresh state machine wrapping `apiClient.getTasks`
 * in a `fetchFn` escape hatch, plus a 60s `setInterval` background poll
 * and a `mcp:resources-updated` window-event listener). This module
 * moves that onto the shared `queryClient`, matching the `/all-data`
 * envelope pattern (`lib/queries/all-data.ts`): one query per
 * `['tasks', project, filters]`, one SSE invalidation choke point
 * (`invalidateTasks()` in `lib/query-client.ts`, called from the
 * debounced dispatcher in `lib/mcp-notifications.ts`), and the same
 * PF-3 poll-gating on SSE health.
 *
 * Scope note: this covers the tasks LIST fetch only. Pagination stays
 * client-side in `tasks-dashboard.tsx` — `GET /tasks` returns the whole
 * set, so page/search/priority slice the cached full list in memory (the
 * PF-1 clamp). Only the server-side filters (status / assignment /
 * creator) parameterize the fetch, so only they key the query.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { apiClient, type Task, type TaskFilters } from "../api"
import { projectContext } from "../project-context"
import { tasksQueryKey } from "../query-client"
import { useServerStore } from "../stores/server-store"
import { useSseHealthy } from "../stores/data-store"

/**
 * The tasks-list background poll interval (ms). Safety net BEHIND the
 * live-update SSE stream — suppressed while SSE is healthy (PF-3), see
 * the `refetchInterval` gating below. 60s matches the interval the
 * retired `REFRESH_INTERVAL` `setInterval` in tasks-dashboard used.
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
 * The tasks-list query for the given server-side filter snapshot.
 *
 * Gating:
 *   - `enabled` on server connection: `GET /tasks` would fail with no
 *     server, and the pre-migration `fetchFn` short-circuited to an
 *     empty result when disconnected. The `enabled` gate is the direct
 *     equivalent — the query simply doesn't run until a server connects.
 *   - `refetchInterval` gated on `sseHealthy` (PF-3): while the operator
 *     events stream is up it pushes every mutation within ~300ms (via
 *     `invalidateTasks()`), so the interval poll is redundant load and is
 *     suppressed; when the stream is down (the case the poll exists for)
 *     it falls back to a 60s tick.
 *
 * The `NO_SERVER_CONNECTED` catch preserves the pre-migration quirk: a
 * transient disconnect returns an empty list rather than painting the
 * full-page error panel (connection state is owned by the guard).
 */
export function useTasksQuery(
  filters: TaskFilters,
): UseQueryResult<Task[]> {
  const enabled = useIsConnected()
  const sseHealthy = useSseHealthy()
  return useQuery({
    queryKey: tasksQueryKey(projectContext.projectName, filters),
    queryFn: async () => {
      try {
        return await apiClient.getTasks(filters)
      } catch (err) {
        if (err instanceof Error && err.message === "NO_SERVER_CONNECTED") {
          return []
        }
        throw err
      }
    },
    enabled,
    refetchInterval: sseHealthy ? false : AUTO_REFRESH_INTERVAL_MS,
    // Don't keep polling a backgrounded tab — the SSE stream closes on
    // tab-hide anyway, and the poll re-arms when the tab returns.
    refetchIntervalInBackground: false,
  })
}
