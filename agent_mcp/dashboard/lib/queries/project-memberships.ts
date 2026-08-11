"use client"

/**
 * TanStack Query hook for a project's membership list
 * (`GET /agent-mcp/api/router/projects/<name>/memberships`).
 *
 * W6-followup-2 increment G2 (2026-08-11): project-memberships-modal used
 * to fetch via the hand-rolled `useRouterQuery` hook (with its
 * `enabled: open` gate and a `projectName` refetch dep). This module moves
 * the read onto the shared `queryClient`, keyed
 * `['project-memberships', projectName]` (see
 * `projectMembershipsQueryKey`). Router-level, so the second key segment
 * is the project NAME the modal was opened for, not the active project.
 *
 * The `enabled` gate is preserved so the query only fires while the dialog
 * is open (a closed modal must not fetch). Freshness after a membership
 * mutation rides an explicit `invalidateProjectMemberships(projectName)`
 * from each mutation's success handler (add / remove / change-role / undo)
 * — no SSE at the overview.
 *
 * The 403 "sysadmin only" outcome is NOT folded into a `forbidden` flag —
 * `useQuery` surfaces it as a thrown `ApiError` on `error`, and the
 * consumer derives `forbidden` from
 * `error instanceof ApiError && error.status === 403`.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { projectMembershipsQueryKey } from "../query-client"
import { routerApi } from "@/lib/router-api"
import { projectMembershipsUrl } from "@/lib/urls"

export type Role = "operator" | "viewer"

export interface MembershipRow {
  membership_id: string
  user_id?: string
  username?: string
  group_id?: string
  group_name?: string
  role: Role
}

export async function fetchMemberships(
  projectName: string,
  signal?: AbortSignal,
): Promise<MembershipRow[]> {
  const body = await routerApi.request<{ memberships?: MembershipRow[] }>(
    projectMembershipsUrl(projectName),
    signal ? { signal } : {},
  )
  return body.memberships || []
}

/**
 * The project memberships query. `enabled` gates the fetch on the dialog
 * being open (the old `useRouterQuery` `{ enabled: open }`), so a
 * not-yet-open modal stays idle. `retry` is off (the `queryClient`
 * default) so a 403 surfaces immediately as an `ApiError`.
 */
export function useProjectMembershipsQuery(
  projectName: string,
  enabled: boolean,
): UseQueryResult<MembershipRow[]> {
  return useQuery({
    queryKey: projectMembershipsQueryKey(projectName),
    queryFn: ({ signal }) => fetchMemberships(projectName, signal),
    enabled,
  })
}
