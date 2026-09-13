/**
 * Regression guards for the groups-list migration onto TanStack Query.
 *
 * History
 * -------
 * The router-admin surface (users / groups / SSO / memberships /
 * capabilities) rode the hand-rolled `useRouterQuery` hook — a
 * `{data, loading, error, forbidden, refresh}` state machine, the
 * router-admin sibling of the retired `usePagedQuery`.
 *
 * W6-followup F4 moved the GROUPS LIST read (`GET
 * /agent-mcp/api/router/groups`) off `useRouterQuery` and onto the
 * shared TanStack Query `queryClient` via `useGroupsQuery`
 * (`lib/queries/groups.ts`), mirroring the F2 (tasks) / F3 (messages)
 * migrations. Two things make groups deliberately different from the
 * per-project lists, and this guard pins both:
 *
 *   * ROUTER-level key. `groupsQueryKey()` is a bare `['groups']` with
 *     NO project segment (contrast `['tasks', project, …]` /
 *     `['messages', project, …]`) — there is one groups list per router.
 *
 *   * NO SSE poll / invalidation. The groups page renders at the
 *     cross-project overview, which has no operator-events SSE stream, so
 *     freshness after a group mutation rides an explicit
 *     `invalidateGroups()` from each mutation's success handler rather
 *     than the debounced SSE choke point the per-project lists use.
 *
 * W6-followup-2 G2 then migrated the LAST four `useRouterQuery`
 * consumers — SSO, users, project memberships and the per-group
 * capabilities section — onto their own TanStack Query modules
 * (`lib/queries/{sso,users,project-memberships,group-capabilities}.ts`),
 * each with a bare-or-parent-scoped key + `invalidateX()` in
 * `lib/query-client.ts`. With zero consumers left, the hand-rolled hook
 * (`hooks/use-router-query.ts`) and its vitest guard were DELETED —
 * exactly the fully-retired shape of `use-paged-query.ts`. So the guard
 * below now asserts the hook is GONE and every former consumer imports its
 * TanStack replacement instead.
 *
 * These are text-parse regression guards (same convention as the
 * retired `use-paged-query` hook guard); behaviour is verified by the
 * vitest suites (`groups-query-invalidation.test.ts` +
 * `router-admin-queries-invalidation.test.ts` + `groups-dashboard.test.tsx`)
 * plus `npm run build`.
 */

import { describe, expect, it } from "vitest"
import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")

function read(rel: string): string {
  return readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")
}

// ---------- The groups query module + key/invalidation seam ----------

describe("groups query module + key/invalidation seam", () => {
  it("lib/queries/groups.ts rides useQuery keyed by groupsQueryKey", () => {
    // `lib/queries/groups.ts` must exist and ride `useQuery` keyed by
    // `groupsQueryKey` — the same shape as `lib/queries/tasks.ts` /
    // `lib/queries/messages.ts`.
    const src = read("lib/queries/groups.ts")
    expect(
      src.includes("useGroupsQuery"),
      "expected lib/queries/groups.ts to export useGroupsQuery",
    ).toBe(true)
    expect(
      src.includes("useQuery"),
      "expected useGroupsQuery to ride TanStack Query's useQuery",
    ).toBe(true)
    expect(
      src.includes("groupsQueryKey"),
      "expected useGroupsQuery to key on groupsQueryKey (from " +
        "lib/query-client.ts)",
    ).toBe(true)
  })

  it("lib/query-client.ts exposes the router-level groups key and invalidator", () => {
    // `lib/query-client.ts` must own the router-level groups key +
    // invalidator. `groupsQueryKey` is a bare `['groups']` (no project
    // segment — groups are router-level, not per-project), and
    // `invalidateGroups` is the manual freshness choke point the group
    // mutations call.
    const src = read("lib/query-client.ts")
    expect(
      src.includes("export const groupsQueryKey"),
      "expected lib/query-client.ts to export groupsQueryKey",
    ).toBe(true)
    expect(
      src.includes("export function invalidateGroups"),
      "expected lib/query-client.ts to export invalidateGroups()",
    ).toBe(true)
    // The key must carry NO project segment — a bare ['groups'].
    expect(
      /groupsQueryKey\s*=\s*\(\s*\)\s*=>\s*\[\s*GROUPS_KEY\s*\]/.test(src),
      "expected groupsQueryKey() to be a bare [GROUPS_KEY] — groups are " +
        "a ROUTER-level resource and must NOT be project-namespaced like " +
        "tasksQueryKey / messagesQueryKey",
    ).toBe(true)
  })
})

// ---------- The groups page migrated off useRouterQuery --------------

describe("groups-dashboard.tsx migrated to TanStack Query", () => {
  it("imports useGroupsQuery + invalidateGroups and no longer imports use-router-query", () => {
    // W6-followup F4: groups-dashboard.tsx no longer rides
    // `useRouterQuery` for its list — it imports the TanStack
    // `useGroupsQuery` (`lib/queries/groups.ts`) and wires
    // `invalidateGroups` as the post-mutation freshness path.
    //
    // Repointed — NOT weakened — from the old router-query import to
    // the new location: the page must import the TanStack groups query
    // and must NOT `import` the retired hook module (an incidental
    // doc-comment naming the hook to explain lineage is fine).
    const src = read("components/dashboard/groups-dashboard.tsx")
    expect(
      src.includes("useGroupsQuery"),
      "expected groups-dashboard.tsx to import useGroupsQuery (the " +
        "TanStack Query groups-list fetch)",
    ).toBe(true)
    expect(
      src.includes("lib/queries/groups"),
      "expected groups-dashboard.tsx to reference '@/lib/queries/groups'",
    ).toBe(true)
    expect(
      src.includes("invalidateGroups"),
      "expected groups-dashboard.tsx to call invalidateGroups() as the " +
        "post-mutation freshness path (no SSE at the overview)",
    ).toBe(true)
    // A REAL import of the retired hook — NOT the doc-comment mentions
    // that describe the migration's lineage.
    expect(
      /from\s+['"][^'"]*use-router-query['"]/.test(src),
      "expected groups-dashboard.tsx to NOT import '@/hooks/use-router-query' " +
        "after the F4 migration onto TanStack Query",
    ).toBe(false)
  })
})

// ---------- useRouterQuery is fully retired (G2) ----------------------

// Each former `useRouterQuery` consumer → the TanStack Query module it
// now imports (`lib/queries/<name>.ts`). Repointed, NOT weakened: the
// assertion moved from "still imports the hook" to "imports its
// migration target and no longer imports the deleted hook".
const MIGRATED_CONSUMERS: [string, string][] = [
  ["components/dashboard/sso-dashboard.tsx", "lib/queries/sso"],
  ["components/dashboard/users-dashboard.tsx", "lib/queries/users"],
  [
    "components/dashboard/project-memberships-modal.tsx",
    "lib/queries/project-memberships",
  ],
  [
    "components/dashboard/groups/group-capabilities-section.tsx",
    "lib/queries/group-capabilities",
  ],
]

describe("useRouterQuery hook is fully retired (G2)", () => {
  it("hooks/use-router-query.ts and its vitest guard are deleted", () => {
    // W6-followup-2 G2: with the last four consumers migrated onto
    // TanStack Query, the hand-rolled `useRouterQuery` hook + its
    // vitest guard are DELETED — the fully-retired shape of
    // `use-paged-query.ts`. Nothing may import the hook module any
    // more.
    const hook = resolve(DASHBOARD_ROOT, "hooks", "use-router-query.ts")
    expect(
      existsSync(hook),
      "hooks/use-router-query.ts must be DELETED after G2 migrated its " +
        "last consumers (users / SSO / memberships / capabilities) onto " +
        "TanStack Query",
    ).toBe(false)
    const guard = resolve(DASHBOARD_ROOT, "tests", "use-router-query.test.ts")
    expect(
      existsSync(guard),
      "tests/use-router-query.test.ts (the resolveRouterQuery guard) must " +
        "be DELETED alongside the hook it tested",
    ).toBe(false)
  })

  it("every former useRouterQuery consumer imports its TanStack Query module", () => {
    // Every former `useRouterQuery` consumer imports its TanStack
    // Query module (`lib/queries/<name>`) and no longer imports the
    // deleted hook. Incidental doc-comment mentions of the old hook's
    // name (to explain migration lineage) are fine — only a real
    // `import` is forbidden.
    for (const [rel, queryModule] of MIGRATED_CONSUMERS) {
      const src = read(rel)
      expect(
        src.includes(queryModule),
        `expected ${rel} to import '@/${queryModule}' (its TanStack ` +
          "Query replacement for useRouterQuery)",
      ).toBe(true)
      expect(
        /from\s+['"][^'"]*use-router-query['"]/.test(src),
        `expected ${rel} to NOT import '@/hooks/use-router-query' after ` +
          "the G2 migration onto TanStack Query",
      ).toBe(false)
    }
  })

  it("lib/query-client.ts exposes the router-admin keys and invalidators", () => {
    // `lib/query-client.ts` owns the router-level key + invalidator for
    // each migrated resource, matching the groups seam. Users/SSO are
    // single router-level resources (bare keys); memberships/capabilities
    // are keyed by their parent id.
    const src = read("lib/query-client.ts")
    for (const symbol of [
      "export const usersQueryKey",
      "export function invalidateUsers",
      "export const ssoConfigQueryKey",
      "export function invalidateSsoConfig",
      "export const projectMembershipsQueryKey",
      "export function invalidateProjectMemberships",
      "export const groupCapabilitiesQueryKey",
      "export function invalidateGroupCapabilities",
    ]) {
      expect(
        src.includes(symbol),
        `expected lib/query-client.ts to declare \`${symbol}\` (G2 ` +
          "router-admin migration)",
      ).toBe(true)
    }
  })
})
