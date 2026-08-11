"use client"

/**
 * TanStack Query hook for the router-admin users list
 * (`GET /agent-mcp/api/router/users`).
 *
 * W6-followup-2 increment G2 (2026-08-11): users-dashboard used to fetch
 * via the hand-rolled `useRouterQuery` hook. This module moves the LIST
 * read onto the shared `queryClient`, mirroring the groups migration (F4,
 * `lib/queries/groups.ts`): one query keyed `['users']`, with
 * `invalidateUsers()` (`lib/query-client.ts`) as the freshness choke
 * point.
 *
 * ROUTER-level, not per-project (bare `['users']`, no project segment),
 * and NO SSE poll / invalidation — the users page renders at the
 * cross-project overview, which has no operator-events SSE stream. So
 * freshness after a user mutation rides an explicit `invalidateUsers()`
 * from each mutation's success handler, matching the pre-migration
 * `refresh()` calls exactly.
 *
 * The 403 "sysadmin only" outcome that `useRouterQuery` folded into a
 * `forbidden` flag is NOT folded here — `useQuery` surfaces it as a thrown
 * `ApiError` on `error`, and the consumer derives `forbidden` from
 * `error instanceof ApiError && error.status === 403` (see
 * `users-dashboard.tsx`), the same split `groups-dashboard.tsx` uses.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { usersQueryKey } from "../query-client"
import { routerApi } from "@/lib/router-api"
import { routerUsersUrl } from "@/lib/urls"

export interface UserRow {
  user_id: string
  username: string
  email: string | null
  is_sysadmin: boolean
  created_at: string
  last_login_at: string | null
}

interface ListResponse {
  success: boolean
  users: UserRow[]
}

export async function fetchUsers(signal?: AbortSignal): Promise<UserRow[]> {
  const body = await routerApi.request<ListResponse>(
    routerUsersUrl(),
    signal ? { signal } : {},
  )
  return body.users || []
}

/**
 * The router-admin users list query. No `enabled` gate (the overview read
 * is always available); `retry` is off (the `queryClient` default) so a
 * 403 surfaces immediately as an `ApiError` for the consumer's `forbidden`
 * derivation rather than being retried.
 */
export function useUsersQuery(): UseQueryResult<UserRow[]> {
  return useQuery({
    queryKey: usersQueryKey(),
    queryFn: ({ signal }) => fetchUsers(signal),
  })
}
