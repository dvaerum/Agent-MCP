# ADR 0023: Dashboard data layer on TanStack Query

**Status**: Accepted, 2026-08-12. Shipped incrementally across dashboard
releases 5.77.0 → 5.80.0.
**Date**: 2026-08-12
**Builds on**: ADR-0014 (REST admin API — the endpoints the dashboard reads),
ADR-0020 (router is mount-agnostic — the dashboard is a static export behind
`/agent-mcp/__dashboard`).

## Context

The Next.js dashboard (`agent_mcp/dashboard/`) fetched server state through a
hand-rolled stack: a Zustand `data-store` that cached the `/all-data` bulk
envelope, plus two bespoke fetch hooks — `usePagedQuery` (per-list pagination)
and `useRouterQuery` (router-admin resources) — with the SSE dispatcher
(`lib/mcp-notifications.ts`) mutating the store on `resources/updated`.

A six-lens architecture review found this data layer to be the dashboard's
biggest source of real bugs:

- **ST-3 double-sourcing.** The same rows were held in the Zustand store *and*
  refetched by list hooks — two owners of one truth, which drift.
- **ST-4 split-brain live-updates.** An operator's own mutation and the backend
  `resources/updated` echo each poked the cache through different mechanisms, so
  a single change could fetch twice, and stale writes could clobber fresh ones.
- The store was ~500 lines of manual cache/refresh/invalidation logic
  re-implementing what a query library gives for free, with no request
  de-duplication and no coherent invalidation model.

`@tanstack/react-query` had been *declared* as a dependency but never used.

## Decision

**Adopt TanStack Query as the single data layer for all dashboard server
state**, migrated incrementally (one resource per PR, each E2E-gated on a
disposable `vm-dev` instance) so no single change could destabilise the live
update path.

The shape that landed:

1. **One shared `QueryClient` module singleton** (`lib/query-client.ts`). It is
   a documented singleton on purpose (see *Deferred: ST-6* below) so the
   non-React SSE dispatcher can invalidate the exact cache the React tree
   renders.
2. **The `/all-data` bulk envelope is one query** — `useAllDataQuery`
   (`['all-data', project]`) with thin slice selectors (`useAgents`, `useTasks`,
   `useContextRows`, `useActiveAgents`, …). It feeds agents, overview, memories,
   the agent-select dropdown, and send-directive. This replaced the Zustand
   server-cache (which shed ~500 lines).
3. **Per-resource `useQuery`** for the lists that had their own hooks: tasks
   (`['tasks', project, filters]`), messages (`['messages', project, {filters,
   limit, offset}]` — genuinely server-paginated, capped at 500 rows/request),
   groups (`['groups']`), users (`['users']`), sso-config (`['sso-config']`),
   project-memberships (`['project-memberships', name]`), group-capabilities
   (`['group-capabilities', id]`). Per-project resources are project-scoped in
   the key; router-admin resources are bare/parent-scoped.
4. **Single invalidation per mutation.** For `/all-data`-backed pages, a mutation
   success routes through the *same* debounced choke point the SSE echo uses
   (`scheduleDashboardRefresh()`), so the operator's own write and the backend
   `resources/updated` echo **coalesce into exactly one refetch** — and it still
   fires once when SSE is down. Per-resource queries invalidate a prefix-matched
   key (`invalidateTasks`/`invalidateMessages`/…). Router-admin resources have no
   SSE stream at the cross-project overview, so they invalidate manually in the
   mutation success handler.
5. **Typed boundary.** `request<T>()` (`lib/api/client.ts`) takes an optional
   runtime shape guard that throws a `ShapeError` naming the endpoint on a
   malformed 200 — applied on the load-bearing read paths (`/all-data`, tasks,
   system status). `lib/api.ts` was split into per-resource modules under
   `lib/api/*` behind a barrel, and the client is built by a `createApiClient()`
   factory rather than materialised as a God-module import side-effect.
6. **The bespoke hooks are gone.** `usePagedQuery` and `useRouterQuery` were
   deleted once their last consumer migrated.

## Consequences

**Positive.**
- One source of truth per resource; ST-3 and ST-4 are closed at the root.
- Request de-duplication, background refetch, and a coherent invalidation model
  come from the library, not from ~500 lines of hand-rolled store code (deleted).
- A uniform mental model: every server read is a `useQuery`; every mutation
  invalidates a key. New resources follow one pattern.
- Mutations fetch once, not twice (verified in E2E: a memory create triggers
  exactly one `/all-data` fetch; a capability save-then-fast-toggle keeps the
  in-flight edit).

**Negative / cost.**
- Two documented module singletons (`queryClient`, `apiClient`) instead of pure
  per-instance state — the price of letting non-React code (the SSE dispatcher,
  imperative helpers, import-time base-URL bootstrap) reach the same cache/client
  the tree uses.
- Messages pagination stays server-side (the endpoint caps at 500 rows), so its
  key carries `limit`/`offset` and each page is a round-trip (with
  `keepPreviousData` for no-flicker paging) — it does not follow the
  client-side-slice pattern the other lists use.

### Deferred: full per-instance `apiClient` (ST-6) — deliberately NOT pursued

The migration delivered the *real* value of the long-standing "ST-6" goal — a
`createApiClient()` factory, so tests and any future multi-endpoint scope can
instantiate their own client, and the singleton is no longer a God-module import
side-effect. It stops there **by decision**, not by omission.

Making the client genuinely per-instance / React-context-injected was
investigated (2026-08-12) and rejected, because `apiClient` is not merely
"shared" — it is global mutable state representing the **one active backend
connection**, and its `baseUrl` is owned by code that runs *outside* React:

- `lib/project-context.ts` sets `baseUrl` at **import time**, before any provider
  can mount, so the very first fetch hits the right proxy.
- The **persisted** Zustand `server-store` mutates `baseUrl` in five actions to
  switch backends in multi-server mode. The app switches the *active* backend; it
  never runs two live simultaneously.
- Four non-React consumers (the `messages`/`all-data`/`tasks` query functions and
  `data-store`) cannot call a `useApiClient()` hook.

Given that, a provider-owned instance **forks** the client (React tree reads
instance X while `baseUrl` is set on instance Y → wrong-backend fetches, broken
server-switching), and a provider-set ref degenerates to today's eager singleton
plus a swap hazard — for **no user-facing benefit**. The SSE/live-update path
does not even read `apiClient` (it reads `projectContext.baseUrl`).

**Revisit ST-6 only if a genuine need for two concurrent live backends ever
arises** — at which point the correct first step is moving `baseUrl` ownership
out of import-time bootstrap and provider-scoping the persisted store, a
separate and larger project, not a quick refactor.

## Verification

Each increment was gated on all 12 CI checks (incl. the Dashboard build, the
ESLint gate, and 4 Nix VM builds) plus a Firefox-driven E2E on a fresh `vm-dev`
instance: every section renders from the query cache, keyboard detail-open,
create/edit/delete round-trips, SSE live-update reflecting a change with a
*single* fetch, theme toggle, and mobile viewport — with zero dashboard-origin
console errors. A three-reviewer adversarial audit of the finished migration
(release 5.80.0) found no high/critical bugs.

See also `docs/learnings/dashboard-data-layer.md` for the practical "how to work
in this code" companion (query-key conventions, when to invalidate via SSE vs
manually, the single-invalidation-per-mutation pattern, and the `request<T>`
guard).
