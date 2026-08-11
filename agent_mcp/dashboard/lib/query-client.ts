"use client"

/**
 * The single shared TanStack Query client for the whole dashboard.
 *
 * Wave 6 keystone increment 1 (2026-08-11): TanStack Query replaces the
 * hand-rolled `lib/stores/data-store.ts` server-cache for the `/all-data`
 * envelope. This client lives at MODULE scope (not created per-render)
 * for one load-bearing reason: non-React callers need to reach the same
 * cache the React tree reads. Specifically the operator-events SSE
 * dispatcher in `lib/mcp-notifications.ts` calls `invalidateAllData()`
 * below when a `resources/updated` notification arrives — a single
 * invalidation that refetches the one shared `['all-data', project]`
 * query, which is what closes ST-3 (double-sourcing) and ST-4
 * (split-brain live updates). A per-render `new QueryClient()` would give
 * the SSE dispatcher a different cache than the components render from.
 *
 * Defaults:
 *   - staleTime 30s mirrors the old data-store freshness gate, so a
 *     component remount inside the window reuses the cache instead of
 *     refetching.
 *   - refetchOnWindowFocus is OFF: the live-update SSE stream is the
 *     freshness driver; a focus refetch would just add redundant
 *     `/all-data` load (the exact pressure the store's 404-fallback
 *     cascade comment warns about).
 *   - retry is OFF: `ApiClient.request()` already does transparent
 *     exponential-backoff on 5xx (cold-start), so a second RQ retry
 *     layer would stack backoffs.
 */

import { QueryClient } from "@tanstack/react-query"
import { projectContext } from "./project-context"

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: false,
    },
  },
})

/** Query-key root for the `/all-data` bulk envelope. */
export const ALL_DATA_KEY = "all-data" as const

/**
 * Stable query key for the `/all-data` envelope, namespaced by project.
 *
 * Path-prefixed deployments route each project to its own backend, so
 * the cache must not bleed across projects when the operator switches.
 * Standalone (single-tenant) has no project name — key it `standalone`
 * so the tuple shape stays uniform.
 */
export const allDataQueryKey = (projectName: string | null) =>
  [ALL_DATA_KEY, projectName ?? "standalone"] as const

/**
 * Invalidate the active-project `/all-data` query, forcing a single
 * refetch of the mounted query.
 *
 * This is the ONE mutation choke point the SSE dispatcher calls (see
 * `lib/mcp-notifications.ts`). Importable from non-React modules because
 * `queryClient` is a module singleton. Uses the current
 * `projectContext.projectName` so it targets the same key the hooks
 * subscribe to.
 */
export function invalidateAllData(): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: allDataQueryKey(projectContext.projectName),
  })
}

/** Query-key root for the tasks list (`GET /tasks`). */
export const TASKS_KEY = "tasks" as const

/**
 * Stable query key for the tasks list, namespaced by project and keyed
 * on the *server-side* filter snapshot (status / assignment / creator)
 * — the only inputs that actually parameterize `GET /tasks`.
 *
 * Deliberately NOT keyed by page: `GET /tasks` returns the WHOLE task
 * set with no server-side pagination, so page/search/priority are a
 * pure in-memory slice over the cached full list (see
 * `tasks-dashboard.tsx`'s PF-1 clamp). Folding page into the key would
 * fragment the cache and refetch the identical full list on every page
 * step — the opposite of the "single source, one invalidation" shape.
 *
 * The `filters` object is embedded verbatim; React Query hashes it with
 * sorted keys, so two renders producing an equal filter snapshot resolve
 * to the same cache entry. `invalidateTasks()` prefix-matches on
 * `[TASKS_KEY, project]`, so every filter variant is invalidated at once.
 */
export const tasksQueryKey = (
  projectName: string | null,
  filters: object = {},
) => [TASKS_KEY, projectName ?? "standalone", filters] as const

/**
 * Invalidate the active-project tasks list across every filter variant,
 * forcing a single refetch of each mounted tasks query. Prefix-matches
 * `[TASKS_KEY, project]`, so `['tasks', project, {status:'pending'}]` and
 * `['tasks', project, {}]` are both hit.
 *
 * Called from the same SSE choke point as `invalidateAllData()` (see
 * `lib/mcp-notifications.ts`) so a tasks mutation surfaces on the tasks
 * page without its own poll. Importable from non-React modules because
 * `queryClient` is a module singleton.
 */
export function invalidateTasks(): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: [TASKS_KEY, projectContext.projectName ?? "standalone"],
  })
}

/** Query-key root for the messages list (`POST /messages/query`). */
export const MESSAGES_KEY = "messages" as const

/**
 * Stable query key for the messages list, namespaced by project and
 * keyed on the full request snapshot (`{ filters, limit, offset }`).
 *
 * Unlike the tasks list (whose `GET /tasks` returns the WHOLE set, so
 * page stays a client-side slice OUT of the key — see `tasksQueryKey`),
 * the messages list is genuinely SERVER-paginated: `POST /messages/query`
 * takes `limit`/`offset` (default 50, hard-capped at 500 rows per
 * request) and returns a SEPARATE `total` count. There is no way to pull
 * "the whole set" in one call once an inbox exceeds 500 rows, so each
 * page is its own backend round-trip and therefore its own cache entry —
 * `offset`/`limit` MUST be part of the key or paging couldn't refetch.
 * `keepPreviousData` (see `useMessagesQuery`) holds the current page's
 * rows on screen while the next page loads, preserving the old
 * `usePagedQuery` "no flicker on page step" feel.
 *
 * The `params` object is embedded verbatim; React Query hashes it with
 * sorted keys, so two renders producing an equal snapshot resolve to the
 * same entry. `invalidateMessages()` prefix-matches on
 * `[MESSAGES_KEY, project]`, so every page + filter variant is
 * invalidated at once (a new inbound message must surface no matter
 * which page/filter the operator is viewing).
 */
export const messagesQueryKey = (
  projectName: string | null,
  params: object = {},
) => [MESSAGES_KEY, projectName ?? "standalone", params] as const

/**
 * Invalidate the active-project messages list across every page + filter
 * variant, forcing a single refetch of each mounted messages query.
 * Prefix-matches `[MESSAGES_KEY, project]`.
 *
 * Called from the same debounced SSE choke point as `invalidateAllData()`
 * / `invalidateTasks()` (see `lib/mcp-notifications.ts`) so a new inbound
 * message surfaces on the Messages page without its own poll — the retired
 * 60s `setInterval` + `mcp:resources-updated` window listener are replaced
 * by this one coalesced invalidation. Importable from non-React modules
 * because `queryClient` is a module singleton.
 */
export function invalidateMessages(): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: [MESSAGES_KEY, projectContext.projectName ?? "standalone"],
  })
}
