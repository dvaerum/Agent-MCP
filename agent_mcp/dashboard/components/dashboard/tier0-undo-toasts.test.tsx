// @vitest-environment jsdom
//
// The three reversible removals that used to succeed SILENTLY.
//
// All three are correct no-dialog actions — each deletes exactly one
// row and each is re-grantable — but pre-PR none of them told the user
// anything at all on success. Material's confirmation guidance
// ("confirmation isn't necessary when the consequences of an action
// are reversible") only holds if the reversal is actually OFFERED, so
// each now lands a toast, and the two with a clean inverse REST call
// land an Undo.
//
//   * group member remove   → Undo re-POSTs .../groups/<g>/members
//   * project membership    → Undo re-POSTs .../projects/<p>/memberships
//                             WITH the row's original role
//   * alias remove          → plain success toast, NO Undo: the router
//                             exposes only GET + DELETE on
//                             .../projects/<n>/aliases (see
//                             `register_admin_routes` in
//                             agent_mcp/router/admin_api.py) — there is
//                             no create-alias endpoint to invert with,
//                             and the rename path that does mint
//                             aliases would reset `expires_at`. Faking
//                             it would restore a DIFFERENT alias.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { render, cleanup, screen, waitFor, within } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

const requestMock = vi.fn()
vi.mock("@/lib/router-api", () => ({
  routerApi: { request: (...args: unknown[]) => requestMock(...args) },
}))
const fetchOverview = vi.fn().mockResolvedValue(undefined)
vi.mock("@/lib/stores/projects-store", () => ({
  useProjectsStore: (sel: (s: unknown) => unknown) => sel({ fetchOverview }),
}))

import { GroupsDashboard } from "@/components/dashboard/groups-dashboard"
import { ProjectMembershipsModal } from "@/components/dashboard/project-memberships-modal"
import { AliasChipPanel } from "@/components/dashboard/alias-chip-panel"
import { Toaster, __resetToastsForTests } from "@/components/ui/toast"

beforeEach(() => {
  requestMock.mockReset()
  fetchOverview.mockClear()
})
afterEach(() => {
  cleanup()
  __resetToastsForTests()
})

/** Route the shared router-api mock by URL substring. */
function routeBy(handlers: Record<string, unknown>) {
  requestMock.mockImplementation((url: string) => {
    for (const [needle, value] of Object.entries(handlers)) {
      if (url.includes(needle)) {
        return value instanceof Error
          ? Promise.reject(value)
          : Promise.resolve(value)
      }
    }
    return Promise.resolve({})
  })
}

const GROUPS = [
  {
    group_id: "g1",
    name: "devs",
    is_sysadmin: false,
    created_at: "2026-01-01T00:00:00Z",
    member_count: 1,
  },
]

// ---------------------------------------------------------------------
// 1. Group member remove
// ---------------------------------------------------------------------

describe("group member remove", () => {
  async function expandDevs() {
    const u = setupUser()
    render(
      <>
        <GroupsDashboard />
        <Toaster />
      </>,
    )
    await screen.findByRole("heading", { name: "Groups" })
    const table = document.querySelector("table") as HTMLElement
    await u.click(within(table).getByRole("button", { name: "Expand devs" }))
    const panel = (await waitFor(() => {
      const el = document.querySelector('[data-slot="data-table-expanded"]')
      expect(el).toBeTruthy()
      return el
    })) as HTMLElement
    await within(panel).findByRole("button", { name: "Remove alice" })
    return { u, panel }
  }

  it("toasts on success and offers Undo", async () => {
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [{ user_id: "u1", username: "alice", added_at: "x" }] },
      "/groups": { groups: GROUPS },
    })
    const { u, panel } = await expandDevs()

    await u.click(within(panel).getByRole("button", { name: "Remove alice" }))

    await screen.findByText(/Removed alice from devs/i)
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy()
  })

  it("Undo re-POSTs the member back to the group", async () => {
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [{ user_id: "u1", username: "alice", added_at: "x" }] },
      "/groups": { groups: GROUPS },
    })
    const { u, panel } = await expandDevs()
    await u.click(within(panel).getByRole("button", { name: "Remove alice" }))
    await screen.findByRole("button", { name: "Undo" })

    requestMock.mockClear()
    await u.click(screen.getByRole("button", { name: "Undo" }))

    await waitFor(() => {
      const post = requestMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      )
      expect(post, "no POST issued by Undo").toBeTruthy()
      expect(String(post![0])).toContain("/groups/g1/members")
      expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
        user_id: "u1",
      })
    })
  })

  it("Undo of a nested GROUP member re-POSTs group_id, not user_id", async () => {
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [{ group_id: "g2", name: "alice", added_at: "x" }] },
      "/groups": { groups: GROUPS },
    })
    const { u, panel } = await expandDevs()
    await u.click(within(panel).getByRole("button", { name: "Remove alice" }))
    await screen.findByRole("button", { name: "Undo" })

    requestMock.mockClear()
    await u.click(screen.getByRole("button", { name: "Undo" }))

    await waitFor(() => {
      const post = requestMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      )
      expect(post).toBeTruthy()
      expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
        group_id: "g2",
      })
    })
  })

  it("tells the user when the Undo re-add fails", async () => {
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [{ user_id: "u1", username: "alice", added_at: "x" }] },
      "/groups": { groups: GROUPS },
    })
    const { u, panel } = await expandDevs()
    await u.click(within(panel).getByRole("button", { name: "Remove alice" }))
    await screen.findByRole("button", { name: "Undo" })

    requestMock.mockImplementation((url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.reject(new Error("membership already exists"))
        : Promise.resolve({ members: [] }),
    )
    await u.click(screen.getByRole("button", { name: "Undo" }))

    expect(await screen.findByText(/membership already exists/)).toBeTruthy()
    expect(screen.queryByText(/restored/i)).toBeNull()
  })

  it("still routes a FAILED remove to the error toast, with no Undo", async () => {
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [{ user_id: "u1", username: "alice", added_at: "x" }] },
      "/groups": { groups: GROUPS },
    })
    const { u, panel } = await expandDevs()
    requestMock.mockImplementation((url: string, init?: RequestInit) =>
      init?.method === "DELETE"
        ? Promise.reject(new Error("nope"))
        : Promise.resolve({ members: [] }),
    )
    await u.click(within(panel).getByRole("button", { name: "Remove alice" }))

    await screen.findByText("Failed to remove member")
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull()
  })
})

// ---------------------------------------------------------------------
// 2. Project membership remove
// ---------------------------------------------------------------------

describe("project membership remove", () => {
  const ROWS = [
    {
      membership_id: "m1",
      user_id: "u1",
      username: "alice",
      role: "viewer" as const,
    },
  ]

  async function openModal() {
    const u = setupUser()
    render(
      <>
        <ProjectMembershipsModal projectName="acme" open onOpenChange={() => {}} />
        <Toaster />
      </>,
    )
    await screen.findByRole("button", { name: "Remove alice" })
    return u
  }

  it("toasts on success and offers Undo", async () => {
    routeBy({ "/memberships": { memberships: ROWS } })
    const u = await openModal()
    await u.click(screen.getByRole("button", { name: "Remove alice" }))
    await screen.findByText(/Removed alice from acme/i)
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy()
  })

  it("Undo re-grants the SAME role", async () => {
    routeBy({ "/memberships": { memberships: ROWS } })
    const u = await openModal()
    await u.click(screen.getByRole("button", { name: "Remove alice" }))
    await screen.findByRole("button", { name: "Undo" })

    requestMock.mockClear()
    await u.click(screen.getByRole("button", { name: "Undo" }))

    await waitFor(() => {
      const post = requestMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      )
      expect(post, "no POST issued by Undo").toBeTruthy()
      expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
        user_id: "u1",
        role: "viewer",
      })
    })
  })

  it("keeps the inline error path for a FAILED remove", async () => {
    routeBy({ "/memberships": { memberships: ROWS } })
    const u = await openModal()
    requestMock.mockImplementation((url: string, init?: RequestInit) =>
      init?.method === "DELETE"
        ? Promise.reject(new Error("boom"))
        : Promise.resolve({ memberships: ROWS }),
    )
    await u.click(screen.getByRole("button", { name: "Remove alice" }))
    expect(await screen.findByText("boom")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull()
  })
})

// ---------------------------------------------------------------------
// 3. Alias remove — success toast, deliberately NO undo
// ---------------------------------------------------------------------

describe("alias remove", () => {
  const ALIAS = { name: "old-name", expires_at: "2026-09-01T00:00:00Z" }

  it("toasts on success", async () => {
    routeBy({
      "/aliases": { alias: "old-name", project: "acme", expires_at: "", agents: [] },
    })
    const u = setupUser()
    render(
      <>
        <AliasChipPanel
          projectName="acme"
          alias={ALIAS}
          open
          onClose={() => {}}
        />
        <Toaster />
      </>,
    )
    await u.click(await screen.findByRole("button", { name: /Remove alias now/ }))
    expect(await screen.findByText(/Removed alias old-name/i)).toBeTruthy()
  })

  it("offers NO Undo — the router has no create-alias endpoint", async () => {
    routeBy({
      "/aliases": { alias: "old-name", project: "acme", expires_at: "", agents: [] },
    })
    const u = setupUser()
    render(
      <>
        <AliasChipPanel
          projectName="acme"
          alias={ALIAS}
          open
          onClose={() => {}}
        />
        <Toaster />
      </>,
    )
    await u.click(await screen.findByRole("button", { name: /Remove alias now/ }))
    await screen.findByText(/Removed alias old-name/i)
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull()
  })
})
