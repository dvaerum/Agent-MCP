"use client"

/**
 * TanStack Query hook for the router-admin groups list
 * (`GET /agent-mcp/api/router/groups`).
 *
 * W6-followup increment F4 (2026-08-11): groups-dashboard used to fetch
 * via the hand-rolled `useRouterQuery` hook (the router-admin sibling of
 * the retired `usePagedQuery` — a `{data, loading, error, forbidden,
 * refresh}` state machine with its own AbortController race-guard). This
 * module moves the LIST read onto the shared `queryClient`, mirroring the
 * tasks (F2) / messages (F3) migrations: one query keyed `['groups']`,
 * with `invalidateGroups()` (`lib/query-client.ts`) as the freshness
 * choke point.
 *
 * Two ways groups differ from tasks/messages, both deliberate:
 *
 *   - ROUTER-level, not per-project. `groupsQueryKey()` is a bare
 *     `['groups']` with no project segment — there is one groups list per
 *     router (see `groupsQueryKey`).
 *
 *   - NO SSE poll / invalidation. The groups page renders at the
 *     cross-project overview, which has no operator-events SSE stream
 *     (`subscribeMcpNotifications` early-returns for `isOverview`). So
 *     there is neither a `refetchInterval` (tasks/messages gate theirs on
 *     `sseHealthy`) nor a debounced SSE invalidation — freshness rides an
 *     explicit `invalidateGroups()` from each group mutation's success
 *     handler, matching the pre-migration `refresh()` calls exactly.
 *
 * The 403 "sysadmin only" outcome that `useRouterQuery` folded into a
 * `forbidden` flag is NOT folded here — `useQuery` surfaces it as a
 * thrown `ApiError` on `error`, and the consumer derives `forbidden`
 * from `error instanceof ApiError && error.status === 403` (see
 * `groups-dashboard.tsx`). This keeps the hook a plain
 * `UseQueryResult<GroupRow[]>`, same shape as `useTasksQuery` /
 * `useMessagesQuery`.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { groupsQueryKey } from "../query-client"
import {
  fetchGroups,
  type GroupRow,
} from "@/components/dashboard/groups/groups-api"

/**
 * The router-admin groups list query.
 *
 * No `enabled` gate: unlike the per-project lists (which short-circuit
 * while no server is connected), the groups list is a router-admin read
 * that is always available at the overview — `useRouterQuery` fetched it
 * unconditionally on mount, and this preserves that. `retry` is off (the
 * `queryClient` default), so a 403 surfaces immediately as an `ApiError`
 * for the consumer's `forbidden` derivation rather than being retried.
 */
export function useGroupsQuery(): UseQueryResult<GroupRow[]> {
  return useQuery({
    queryKey: groupsQueryKey(),
    queryFn: ({ signal }) => fetchGroups(signal),
  })
}
