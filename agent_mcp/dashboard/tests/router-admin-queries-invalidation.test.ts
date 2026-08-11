// @vitest-environment jsdom
/**
 * W6-followup-2 G2 — the LAST router-admin reads migrated off the
 * hand-rolled `useRouterQuery` hook onto TanStack Query: users, SSO
 * config, per-project memberships and per-group capabilities.
 *
 * These mirror `groups-query-invalidation.test.ts` (F4). Like the groups
 * list they are ROUTER-level reads with NO operator-events SSE stream at
 * the cross-project overview, so freshness after a mutation rides an
 * explicit `invalidateX()` from the mutation success handler — the manual
 * analog of the tasks/messages SSE choke point.
 *
 * This pins, for each resource:
 *   1. Its query-key shape — bare `['users']` / `['sso-config']` for the
 *      single router-level resources; `[key, id]` for the per-parent ones
 *      (project name / group id).
 *   2. A seeded cache entry is served fresh, and `invalidateX()` marks it
 *      stale (a mounted query refetches after a mutation).
 *   3. `invalidateX()` does NOT touch unrelated queries.
 */
import { describe, expect, it, afterEach } from "vitest"
import {
  queryClient,
  usersQueryKey,
  invalidateUsers,
  ssoConfigQueryKey,
  invalidateSsoConfig,
  projectMembershipsQueryKey,
  invalidateProjectMemberships,
  groupCapabilitiesQueryKey,
  invalidateGroupCapabilities,
  tasksQueryKey,
} from "@/lib/query-client"

afterEach(() => {
  queryClient.clear()
})

function isStale(key: readonly unknown[]): boolean {
  const found = queryClient.getQueryCache().findAll({ queryKey: key })
  expect(found.length).toBe(1)
  return found[0]!.isStale()
}

describe("usersQueryKey / invalidateUsers", () => {
  it("keys the router-level users list as a bare ['users']", () => {
    expect(usersQueryKey()).toEqual(["users"])
  })

  it("serves a seeded users entry fresh, then invalidateUsers() marks it stale", async () => {
    queryClient.setQueryData(usersQueryKey(), [])
    expect(isStale(usersQueryKey())).toBe(false)
    await invalidateUsers()
    expect(isStale(usersQueryKey())).toBe(true)
  })

  it("invalidateUsers() leaves unrelated queries (tasks) untouched", async () => {
    queryClient.setQueryData(tasksQueryKey("proj", {}), [])
    queryClient.setQueryData(usersQueryKey(), [])
    await invalidateUsers()
    expect(isStale(["tasks", "proj", {}])).toBe(false)
  })
})

describe("ssoConfigQueryKey / invalidateSsoConfig", () => {
  it("keys the router SSO config as a bare ['sso-config']", () => {
    expect(ssoConfigQueryKey()).toEqual(["sso-config"])
  })

  it("serves a seeded SSO entry fresh, then invalidateSsoConfig() marks it stale", async () => {
    queryClient.setQueryData(ssoConfigQueryKey(), { mode: "builtin" })
    expect(isStale(ssoConfigQueryKey())).toBe(false)
    await invalidateSsoConfig()
    expect(isStale(ssoConfigQueryKey())).toBe(true)
  })
})

describe("projectMembershipsQueryKey / invalidateProjectMemberships", () => {
  it("keys memberships as ['project-memberships', projectName]", () => {
    expect(projectMembershipsQueryKey("alpha")).toEqual([
      "project-memberships",
      "alpha",
    ])
  })

  it("invalidateProjectMemberships(name) marks only that project's entry stale", async () => {
    queryClient.setQueryData(projectMembershipsQueryKey("alpha"), [])
    queryClient.setQueryData(projectMembershipsQueryKey("beta"), [])
    expect(isStale(projectMembershipsQueryKey("alpha"))).toBe(false)

    await invalidateProjectMemberships("alpha")

    expect(isStale(projectMembershipsQueryKey("alpha"))).toBe(true)
    // A different project's memberships must stay fresh — the key is
    // scoped by project name.
    expect(isStale(projectMembershipsQueryKey("beta"))).toBe(false)
  })
})

describe("groupCapabilitiesQueryKey / invalidateGroupCapabilities", () => {
  it("keys capabilities as ['group-capabilities', groupId]", () => {
    expect(groupCapabilitiesQueryKey("g1")).toEqual([
      "group-capabilities",
      "g1",
    ])
  })

  it("invalidateGroupCapabilities(id) marks only that group's entry stale", async () => {
    queryClient.setQueryData(groupCapabilitiesQueryKey("g1"), [])
    queryClient.setQueryData(groupCapabilitiesQueryKey("g2"), [])
    expect(isStale(groupCapabilitiesQueryKey("g1"))).toBe(false)

    await invalidateGroupCapabilities("g1")

    expect(isStale(groupCapabilitiesQueryKey("g1"))).toBe(true)
    expect(isStale(groupCapabilitiesQueryKey("g2"))).toBe(false)
  })
})
