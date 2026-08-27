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

/** Query-key root for the schedules list (`GET /schedules`). */
export const SCHEDULES_KEY = "schedules" as const

/**
 * Stable query key for the schedules list, namespaced by project.
 *
 * Deliberately NOT parameterized by filters (contrast `tasksQueryKey`):
 * `GET /schedules` returns the WHOLE set with no server-side filters, and
 * both the agent and status filters in `schedules-dashboard.tsx` are
 * client-side over the full fetched list — matching the messages/tasks
 * "bare project key" shape where the endpoint has no server-side
 * parameters, not the filter-embedded variant `tasksQueryKey` uses.
 */
export const schedulesQueryKey = (projectName: string | null) =>
  [SCHEDULES_KEY, projectName ?? "standalone"] as const

/**
 * Invalidate the active-project schedules list, forcing a refetch of the
 * mounted schedules query. Joins the same debounced SSE choke point as
 * `invalidateTasks()` / `invalidateMessages()` (see
 * `lib/mcp-notifications.ts`) so a schedule mutation OR a background
 * directive fire (a separate backend change publishes a
 * `resources/updated` notification when a scheduled directive fires)
 * surfaces on the Schedules page without its own poll.
 */
export function invalidateSchedules(): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: [SCHEDULES_KEY, projectContext.projectName ?? "standalone"],
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

/** Query-key root for the router-admin groups list (`GET /router/groups`). */
export const GROUPS_KEY = "groups" as const

/**
 * Stable query key for the router-admin groups list.
 *
 * Deliberately NOT project-namespaced (contrast `tasksQueryKey` /
 * `messagesQueryKey`, which carry a project segment). Groups are a
 * ROUTER-level resource — `GET /agent-mcp/api/router/groups` lives under
 * the router-admin root, not under any single project's backend — so a
 * bare `['groups']` is the correct scope. There is exactly one groups
 * list per router, shared across every project the operator can see.
 */
export const groupsQueryKey = () => [GROUPS_KEY] as const

/**
 * Invalidate the router-admin groups list, forcing a refetch of the
 * mounted groups query.
 *
 * Unlike `invalidateTasks()` / `invalidateMessages()`, this is NOT wired
 * into the debounced SSE choke point in `lib/mcp-notifications.ts`: the
 * groups page renders at the cross-project overview, which has no
 * operator-events SSE stream (`subscribeMcpNotifications` early-returns
 * for `isOverview`). Freshness after a group mutation therefore rides an
 * explicit call from the mutation success handler (create / edit /
 * add-member / remove-member / delete) — see `groups-dashboard.tsx`. This
 * is the faithful router-admin analog of the SSE-driven invalidation the
 * per-project lists use. Importable from non-React modules because
 * `queryClient` is a module singleton.
 */
export function invalidateGroups(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: groupsQueryKey() })
}

/**
 * Router-admin resources migrated off the hand-rolled `useRouterQuery`
 * hook in W6-followup-2 G2 (2026-08-11) — users, SSO config, per-project
 * memberships and per-group capabilities. They share the groups list's
 * shape (see `groupsQueryKey` / `invalidateGroups` above): ROUTER-level
 * reads that live at the cross-project overview, which has NO
 * operator-events SSE stream (`subscribeMcpNotifications` early-returns
 * for `isOverview`). So none of them are wired into the debounced SSE
 * choke point in `lib/mcp-notifications.ts`; freshness after a mutation
 * rides an explicit `invalidateX()` from the mutation's success handler —
 * the faithful analog of the old `useRouterQuery` `refresh()`.
 *
 * Users and SSO are single router-level resources → bare keys
 * (`['users']` / `['sso-config']`). Memberships and capabilities are
 * per-parent (a project name / a group id) → the id is the second key
 * segment, mirroring the per-project task/message keys but scoped to the
 * router-admin parent rather than a project backend.
 */

/** Query-key root for the router-admin users list (`GET /router/users`). */
export const USERS_KEY = "users" as const

/**
 * Stable query key for the router-admin users list. Like `groupsQueryKey`
 * (and unlike `tasksQueryKey` / `messagesQueryKey`) this is a bare
 * `['users']` with NO project segment — users are a ROUTER-level resource
 * shared across every project the operator can see.
 */
export const usersQueryKey = () => [USERS_KEY] as const

/**
 * Invalidate the router-admin users list, forcing a refetch of the mounted
 * users query. Called from each user mutation's success handler (create /
 * edit / delete) — the router-admin analog of the SSE-driven invalidation
 * the per-project lists use (there is no SSE at the overview).
 */
export function invalidateUsers(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: usersQueryKey() })
}

/** Query-key root for the router SSO config (`GET /router/sso/config`). */
export const SSO_CONFIG_KEY = "sso-config" as const

/**
 * Stable query key for the router SSO config. A bare `['sso-config']` —
 * one config per router, no project segment. The dashboard's SSO view is
 * read-only today (writes travel via env vars / the nix module, see
 * `sso-dashboard.tsx`), so `invalidateSsoConfig()` has no in-app caller
 * yet; it exists for uniformity + so a future editable SSO surface has the
 * same freshness seam as its siblings.
 */
export const ssoConfigQueryKey = () => [SSO_CONFIG_KEY] as const

/** Invalidate the router SSO config query, forcing a refetch. */
export function invalidateSsoConfig(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: ssoConfigQueryKey() })
}

/** Query-key root for a project's memberships
 *  (`GET /router/projects/<name>/memberships`). */
export const PROJECT_MEMBERSHIPS_KEY = "project-memberships" as const

/**
 * Stable query key for one project's membership list, keyed by project
 * NAME (the identifier the router-admin endpoint routes on). Router-level,
 * so the second segment is the project name rather than a
 * `projectContext`-derived active project — the memberships modal can be
 * opened for any project from the overview.
 */
export const projectMembershipsQueryKey = (projectName: string) =>
  [PROJECT_MEMBERSHIPS_KEY, projectName] as const

/**
 * Invalidate one project's membership list, forcing a refetch of the
 * mounted memberships query. Called from each membership mutation's
 * success handler (add / remove / change-role / undo).
 */
export function invalidateProjectMemberships(
  projectName: string,
): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: projectMembershipsQueryKey(projectName),
  })
}

/** Query-key root for a group's capabilities
 *  (`GET /router/groups/<id>/capabilities`). */
export const GROUP_CAPABILITIES_KEY = "group-capabilities" as const

/**
 * Stable query key for one group's capability set, keyed by group id.
 * Router-level, second segment is the group id.
 */
export const groupCapabilitiesQueryKey = (groupId: string) =>
  [GROUP_CAPABILITIES_KEY, groupId] as const

/**
 * Invalidate one group's capability set, forcing a refetch of the mounted
 * capabilities query. Called from the capability-save (PUT) success
 * handler — the checklist also updates optimistically from the PUT
 * response, and this reconciles the cache so a remount is fresh.
 */
export function invalidateGroupCapabilities(groupId: string): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: groupCapabilitiesQueryKey(groupId),
  })
}
