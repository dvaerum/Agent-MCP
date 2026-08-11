"use client"

/**
 * TanStack Query hook for a group's capability set
 * (`GET /agent-mcp/api/router/groups/<id>/capabilities`).
 *
 * W6-followup-2 increment G2 (2026-08-11): group-capabilities-section used
 * to fetch via the hand-rolled `useRouterQuery` hook. This module moves
 * the read onto the shared `queryClient`, keyed
 * `['group-capabilities', groupId]` (see `groupCapabilitiesQueryKey`).
 *
 * The section keeps HEAVY optimistic local state (a dirty-tracked
 * checklist that `save()` writes straight from the PUT response, avoiding
 * an extra GET). This hook only owns the INITIAL GET — the consumer folds
 * its `{data, isPending, error}` into the same local `loaded / selected /
 * forbidden` state the old inline `load()` used, and reconciles the cache
 * after a save via `invalidateGroupCapabilities(groupId)`. `isPending`
 * (not `isFetching`) drives the loading banner so a post-save background
 * refetch does not flash the checklist away.
 *
 * The 403 "sysadmin only" outcome is NOT folded into a `forbidden` flag
 * here — `useQuery` surfaces it as a thrown `ApiError` on `error`, and the
 * consumer derives `forbidden` from
 * `error instanceof ApiError && error.status === 403`.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { groupCapabilitiesQueryKey } from "../query-client"
import { routerApi } from "@/lib/router-api"
import { routerGroupCapabilitiesUrl } from "@/lib/urls"

export async function fetchGroupCapabilities(
  groupId: string,
  signal?: AbortSignal,
): Promise<string[]> {
  const body = await routerApi.request<{ capabilities?: string[] }>(
    routerGroupCapabilitiesUrl(groupId),
    signal ? { signal } : {},
  )
  return body.capabilities ?? []
}

/**
 * The per-group capabilities query. `retry` is off (the `queryClient`
 * default) so a 403 surfaces immediately as an `ApiError` for the
 * consumer's `forbidden` derivation rather than being retried.
 */
export function useGroupCapabilitiesQuery(
  groupId: string,
): UseQueryResult<string[]> {
  return useQuery({
    queryKey: groupCapabilitiesQueryKey(groupId),
    queryFn: ({ signal }) => fetchGroupCapabilities(groupId, signal),
  })
}
