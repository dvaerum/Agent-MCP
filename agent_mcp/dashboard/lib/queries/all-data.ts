"use client"

/**
 * TanStack Query hooks for the `/all-data` bulk envelope.
 *
 * Wave 6 keystone increment 1 (2026-08-11): this module is the single
 * source of truth for the `/all-data` envelope that used to live in the
 * hand-rolled `lib/stores/data-store.ts` server-cache. Every consumer
 * that previously read `useDataStore().data` (or a derived selector
 * method) now reads one of the hooks below, all of which resolve to the
 * SAME `['all-data', project]` query — so there is exactly one fetch,
 * one cache, and one SSE invalidation choke point (fixes ST-3
 * double-sourcing + ST-4 split-brain live updates).
 *
 * Deliberately NOT migrated here (later increments):
 *   - tasks-dashboard / messages-dashboard already own their own
 *     `usePagedQuery` fetchers — they never read this envelope.
 *   - ST-5 (per-resource api.ts split + getMemories fix) and ST-6
 *     (per-instance apiClient) remain deferred.
 *   - The prompt-book catalogue is a SEPARATE cadence + endpoint; it
 *     stays in `lib/stores/data-store.ts` (zustand), not here.
 */

import { useMemo } from "react"
import { useQuery, type UseQueryResult } from "@tanstack/react-query"
import { apiClient, type Agent, type Task } from "../api"
import { projectContext } from "../project-context"
import { allDataQueryKey, queryClient } from "../query-client"
import { useServerStore } from "../stores/server-store"
import {
  useSseHealthy,
  type AllData,
  type ContextRow,
} from "../stores/data-store"
import {
  normalizeAgentId,
  selectTasks,
  selectActions,
  type ActionRecord,
} from "../stores/selectors"

export type { AllData, ContextRow }

// Stable empty singletons — a fresh `[]` from a selector on every call
// defeats reference equality and forces a re-render each time, so the
// no-data path returns one frozen shared array. (Carried over from the
// old data-store scoped selectors.)
const EMPTY_AGENTS: readonly Agent[] = Object.freeze([])
const EMPTY_TASKS: readonly Task[] = Object.freeze([])

/**
 * The `/all-data` background poll interval (ms). This is the safety net
 * BEHIND the live-update SSE stream — see `useAllDataQuery` for the PF-3
 * gating that suppresses it while SSE is healthy. 60s matches the
 * interval the retired `startDataStoreAutoRefresh` used.
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
 * The one shared `/all-data` query. Every hook in this module funnels
 * through here, so React Query dedupes them into a single fetch + cache
 * entry keyed by project.
 *
 * Gating:
 *   - `enabled` on server connection: the fetch would 500/abort with no
 *     server, and the old consumers guarded their `fetchAllData()` the
 *     same way.
 *   - `refetchInterval` gated on `sseHealthy` (PF-3): when the operator
 *     events stream is up it pushes every mutation within ~300ms, so the
 *     interval poll is pure redundant load and is suppressed; when the
 *     stream is down (the case the poll exists for) it falls back to a
 *     60s tick. This replaces the retired `startDataStoreAutoRefresh`
 *     interval, moving the same PF-3 behaviour onto the query.
 */
export function useAllDataQuery(): UseQueryResult<AllData> {
  const enabled = useIsConnected()
  const sseHealthy = useSseHealthy()
  return useQuery({
    queryKey: allDataQueryKey(projectContext.projectName),
    queryFn: () => apiClient.getAllData(),
    enabled,
    refetchInterval: sseHealthy ? false : AUTO_REFRESH_INTERVAL_MS,
    // Don't keep polling a backgrounded tab — the SSE stream closes on
    // tab-hide anyway, and the poll re-arms when the tab returns.
    refetchIntervalInBackground: false,
  })
}

/** The whole envelope (or undefined before the first load). */
export function useAllData(): AllData | undefined {
  return useAllDataQuery().data
}

/**
 * List-page status shim so consumers keep the `loading` / `isRefreshing`
 * / `error` / `refresh` shape the old data-store exposed.
 *
 *   - `loading`: initial load (fetching with no cached data yet).
 *   - `isRefreshing`: a background refetch while data is already shown.
 *   - `error`: last fetch error message, or null.
 *   - `refresh`: force an immediate refetch (the manual Refresh button).
 */
export interface AllDataStatus {
  loading: boolean
  isRefreshing: boolean
  error: string | null
  // Awaitable so mutation handlers can `await refresh()` and know the
  // row/list reflects the change before they resolve (the old
  // `await refreshData()` contract).
  refresh: () => Promise<void>
}

export function useAllDataStatus(): AllDataStatus {
  const q = useAllDataQuery()
  return {
    loading: q.isLoading,
    isRefreshing: q.isFetching && !q.isLoading,
    error: q.error ? (q.error as Error).message : null,
    refresh: async () => {
      await q.refetch()
    },
  }
}

/** The agents array (stable empty ref when no data is loaded yet). */
export function useAgents(): readonly Agent[] {
  return useAllDataQuery().data?.agents ?? EMPTY_AGENTS
}

/** The tasks array (stable empty ref when no data is loaded yet). */
export function useTasks(): readonly Task[] {
  return useAllDataQuery().data?.tasks ?? EMPTY_TASKS
}

/** The context (memory) rows. */
export function useContextRows(): readonly ContextRow[] {
  const data = useAllDataQuery().data
  return (data?.context as ContextRow[] | undefined) ?? EMPTY_CONTEXT
}
const EMPTY_CONTEXT: readonly ContextRow[] = Object.freeze([])

/**
 * Agents minus terminated rows — the live fleet. Replaces the store's
 * `getActiveAgents()` (predicate: `status !== 'terminated'`). Memoised
 * on the agents array so the returned array is referentially stable
 * across unrelated re-renders.
 */
export function useActiveAgents(): Agent[] {
  const agents = useAllDataQuery().data?.agents
  return useMemo(
    () => (agents ?? []).filter((a) => a.status !== "terminated"),
    [agents],
  )
}

/**
 * Tasks this agent has touched: assigned to them OR they recorded an
 * action against. Pure port of the old `getAgentTasks` store selector —
 * composes `selectTasks`/`selectActions` from lib/stores/selectors.ts.
 */
export function selectAgentTasks(
  data: AllData | undefined,
  agentId: string,
): Task[] {
  if (!data) return []
  const assignedTasks = selectTasks(data.tasks, { assignedTo: agentId })
  const agentActions = selectActions(data.actions as ActionRecord[], {
    agentId,
  })
  const workedOnTaskIds = new Set<string>()
  agentActions.forEach((action) => {
    if (typeof action.task_id === "string") workedOnTaskIds.add(action.task_id)
  })
  const workedOnTasks = selectTasks(data.tasks, {
    taskIdIn: workedOnTaskIds,
    notAssignedTo: agentId,
  })
  const seen = new Set<string>()
  const merged: Task[] = []
  for (const t of [...assignedTasks, ...workedOnTasks]) {
    if (!seen.has(t.task_id)) {
      seen.add(t.task_id)
      merged.push(t)
    }
  }
  return merged
}

/** Hook wrapper around `selectAgentTasks`, memoised on the envelope. */
export function useAgentTasks(agentId: string): Task[] {
  const data = useAllDataQuery().data
  return useMemo(() => selectAgentTasks(data, agentId), [data, agentId])
}

/**
 * Resolve an agent row by id from a loaded envelope, tolerating the
 * `agent_` prefix + Admin/admin casing (pure port of the store's
 * `getAgent`). Exported for imperative + test reach.
 */
export function selectAgent(
  data: AllData | undefined,
  agentId: string,
): Agent | undefined {
  if (!data) return undefined
  const normalized = normalizeAgentId(agentId)
  if (normalized === "admin") {
    return data.agents.find((a) => a.agent_id === "Admin")
  }
  return data.agents.find((a) => a.agent_id === normalized)
}

// ── imperative (non-hook) access ────────────────────────────────────
//
// For callers outside a React render (event handlers reading a one-shot
// value). Reads the current cache snapshot for the active project.

/** The cached envelope for the active project, or undefined. */
export function getAllDataCached(): AllData | undefined {
  return queryClient.getQueryData<AllData>(
    allDataQueryKey(projectContext.projectName),
  )
}

/**
 * The bearer token for an agent, from the cached envelope. Replaces the
 * store's imperative `getAgentToken` (used by the prompt-book "run"
 * handler outside a hook).
 */
export function getAgentTokenCached(agentId: string): string | undefined {
  return selectAgent(getAllDataCached(), agentId)?.auth_token
}
