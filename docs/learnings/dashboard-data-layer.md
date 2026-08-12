# Working in the dashboard data layer (TanStack Query)

Practical companion to [ADR-0023](../adr/0023-dashboard-data-layer-tanstack-query.md),
which records *why* the dashboard's server state moved to TanStack Query. This
file is *how to work in it* without re-introducing the bugs the migration
removed. It does not restate the decision — read the ADR for that.

## Reading server state

Every server read is a `useQuery`. Don't add a `useEffect(fetch)` or a new
Zustand slice for server data.

- `/all-data` envelope (agents, tasks, context, overview, agent-select,
  send-directive): use the slice hooks in `lib/queries/all-data.ts`
  (`useAgents`, `useTasks`, `useContextRows`, `useActiveAgents`, …). One fetch
  backs all of them.
- A resource with its own list (tasks, messages, groups, users, sso,
  project-memberships, group-capabilities): use its `lib/queries/<resource>.ts`
  hook.

### Query-key conventions

- **Per-project** resources carry the project: `['tasks', project, filters]`,
  `['messages', project, {filters, limit, offset}]`, `['all-data', project]`.
- **Router-admin** resources are bare or parent-scoped: `['groups']`,
  `['users']`, `['sso-config']`, `['project-memberships', name]`,
  `['group-capabilities', id]`.
- A key must contain every input the fetch depends on (project, server-side
  filters) and **must not** contain volatile values that would thrash the cache.
  Client-side pagination (tasks) deliberately keeps `page` *out* of the key —
  the whole set is fetched once and sliced in memory. Messages is the exception:
  it is genuinely server-paginated (500-row cap), so `limit`/`offset` are in the
  key and each page is a round-trip.

## Mutating: always invalidate, and invalidate once

After any create/update/delete, the corresponding query must be invalidated or
the UI goes stale. **How** depends on whether the resource has an SSE stream:

- **`/all-data`-backed pages (agents, memories, overview)** have an SSE
  `resources/updated` echo. Route the mutation's refresh through the shared
  debounce **`scheduleDashboardRefresh()`** (`lib/mcp-notifications.ts`) — do
  **not** also call an imperative `refreshData()`. The operator's own write and
  the SSE echo then ride the same debounce timer and **coalesce into exactly one
  refetch**; it still fires once when SSE is down. (Calling both is the ST-4
  double-fetch bug — see ADR-0023.)
- **Per-resource query pages (tasks, messages)** call the prefix-matched
  `invalidateTasks()`/`invalidateMessages()` (`lib/query-client.ts`).
- **Router-admin pages (users, sso, groups, project-memberships,
  group-capabilities)** have **no** SSE at the cross-project overview — invalidate
  **manually** in the mutation success handler (`invalidateUsers()`,
  `invalidateGroupCapabilities(id)`, …). There is no echo to rely on.

### Optimistic edits + background refetch: guard against clobber

If a page updates local state optimistically *and* triggers a background refetch
(e.g. group-capabilities: optimistic checklist + `invalidateGroupCapabilities`),
the refetch's resync effect must **not** overwrite local state while the user is
mid-edit. Gate the resync on a `dirty` check (read dirtiness via a ref so it
isn't an effect dependency) — otherwise a fast edit made within one round-trip of
save is silently discarded. (This was a real audit finding, fixed in 5.80.0.)

## Types at the seam

`request<T>()` (`lib/api/client.ts`) takes an optional third arg: a runtime shape
guard. **Pass one on any read path whose result feeds the cache/consumers** — it
throws a `ShapeError` naming the endpoint on a malformed 200, instead of letting
the mismatch crash a consumer deep in the tree. The load-bearing reads
(`/all-data`, tasks, system-status) have guards; mirror `allDataGuard` /
`taskGuards` for new ones. Also null-guard array access on envelope selectors
(`data.agents?.find(...)`) — a guard at the seam and a `?.` at the use site are
belt-and-braces, and both were needed to close the audit's crash-path finding.

The API client is per-resource modules under `lib/api/*` behind a barrel
(`lib/api/index.ts`); build instances with `createApiClient()`. There is one
shared `apiClient` singleton on purpose — **do not** try to make it
per-instance/context-injected without reading the "Deferred: ST-6" section of
ADR-0023 first (the `baseUrl` is owned by import-time bootstrap + a persisted
store, and forking the instance breaks server-switching for no benefit).

## The SSE dispatcher is non-React

`lib/mcp-notifications.ts` runs outside the React tree. It invalidates through
the **module-singleton** `queryClient` (that's why the singleton exists) and
reads `projectContext.baseUrl` (not `apiClient`) for the event-stream URL. When
adding a new SSE-driven invalidation, add it to the existing debounced choke
point so it coalesces with operator writes.

## Cross-language test guards (don't get surprised by this)

A refactor that only touches `agent_mcp/dashboard/` can still turn Python CI red:
several `tests/test_dashboard_*.py` guards grep the dashboard source (via
`tests/dashboard_sources.py`) to pin conventions. If you move/rename/delete a
symbol they pin, **run `pytest tests/test_dashboard_*.py -q` before pushing** and
repoint the guard to the new location — never weaken it. This bit every increment
of the migration at least once.
