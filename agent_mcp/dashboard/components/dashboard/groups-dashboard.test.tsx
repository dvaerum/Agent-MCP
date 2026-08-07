// @vitest-environment jsdom
//
// Groups page — shared-scaffold migration guard.
//
// Before this migration `groups-dashboard.tsx` hand-rolled its header,
// spinner, "Sysadmin only" 403 card, list-error line, empty text, card
// accordion — AND a *reinvented* toast (a local
// `useState<string|null>` rendered as a green <div>, a same-name
// shadow of `@/components/ui/toast` that it never imported). These
// tests pin the migrated behaviour: the page renders through
// <DataTablePage>, the 403 panel is the scaffold's, mutation/load
// failures reach the REAL shared toast (asserted through the real
// <Toaster /> portal), and the accordion still expands into the
// members + capabilities panel.
//
// jsdom renders BOTH halves of <ResponsiveDataTable> (CSS can't hide
// anything here), so every query is scoped to the desktop <table>.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { render, cleanup, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { ApiError } from "@/lib/api"

// Radix Dialog locks pointer-events on <body>; disable the guard.
const ue = () => userEvent.setup({ pointerEventsCheck: 0 })

const requestMock = vi.fn()
vi.mock("@/lib/router-api", () => ({
  routerApi: { request: (...args: unknown[]) => requestMock(...args) },
}))

import { GroupsDashboard } from "@/components/dashboard/groups-dashboard"
import { Toaster } from "@/components/ui/toast"

const GROUPS = [
  {
    group_id: "g1",
    name: "devs",
    is_sysadmin: false,
    created_at: "2026-01-01T00:00:00Z",
    member_count: 2,
  },
  {
    group_id: "g2",
    name: "admins",
    is_sysadmin: true,
    created_at: "2026-01-02T00:00:00Z",
    member_count: 1,
  },
]

/** Route the mock by URL so one handler serves list + members + caps. */
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

const renderPage = () =>
  render(
    <>
      <GroupsDashboard />
      <Toaster />
    </>,
  )

const desktopTable = () => document.querySelector("table") as HTMLElement

beforeEach(() => {
  requestMock.mockReset()
})
afterEach(() => cleanup())

describe("<GroupsDashboard> on the shared scaffold", () => {
  it("renders the scaffold header + a row per group", async () => {
    routeBy({ "/groups": { groups: GROUPS } })
    renderPage()

    await screen.findByRole("heading", { name: "Groups" })
    expect(
      screen.getByText(
        "Group operators for bulk project access — supports nesting",
      ),
    ).toBeTruthy()
    const table = within(desktopTable())
    expect(table.getByText("devs")).toBeTruthy()
    expect(table.getByText("admins")).toBeTruthy()
    expect(table.getByText("2 members")).toBeTruthy()
    expect(table.getByText("1 member")).toBeTruthy()
  })

  it("renders the scaffold's centralized 'Sysadmin only' panel on 403", async () => {
    routeBy({ "/groups": new ApiError(403, "forbidden", "") })
    renderPage()

    await screen.findByText("Sysadmin only")
    expect(
      screen.getByText("You need sysadmin privileges to view this page."),
    ).toBeTruthy()
    // The retired hand-rolled copy read "Ask a sysadmin to view …".
    expect(screen.queryByText(/Ask a sysadmin/)).toBeNull()
  })

  it("renders the scaffold's empty state when there are no groups", async () => {
    routeBy({ "/groups": { groups: [] } })
    renderPage()

    await screen.findByText("No groups yet")
    expect(document.querySelector('[data-slot="empty-state"]')).toBeTruthy()
  })

  it("shows the scaffold's error panel when the list fails to load", async () => {
    routeBy({ "/groups": new Error("boom") })
    renderPage()

    await screen.findByText("Connection Error")
    expect(screen.getByText("boom")).toBeTruthy()
  })

  it("expands a row and reports member-load failure through the SHARED toast", async () => {
    const u = ue()
    // Capabilities 403s (non-sysadmin) so only the member failure
    // reaches the toast surface.
    routeBy({
      "/capabilities": new ApiError(403, "forbidden", ""),
      "/members": new Error("members exploded"),
      "/groups": { groups: GROUPS },
    })
    renderPage()

    await screen.findByRole("heading", { name: "Groups" })
    await u.click(
      within(desktopTable()).getByRole("button", { name: "Expand devs" }),
    )

    // The expansion row is the scaffold's colSpan sibling row.
    await waitFor(() =>
      expect(
        document.querySelector('[data-slot="data-table-expanded"]'),
      ).toBeTruthy(),
    )
    // The failure went to the shared toast, NOT to a local inline
    // banner (the retired `setMemberError`).
    await screen.findByText("members exploded")
    expect(screen.getByText("Failed to load members")).toBeTruthy()
  })

  it("routes a capabilities save through the SHARED toast (retiring the shadow toast)", async () => {
    const u = ue()
    routeBy({
      "/capabilities": { capabilities: [] },
      "/members": { members: [] },
      "/groups": { groups: GROUPS },
    })
    renderPage()

    await screen.findByRole("heading", { name: "Groups" })
    await u.click(
      within(desktopTable()).getByRole("button", { name: "Expand devs" }),
    )

    const panel = (await waitFor(() => {
      const el = document.querySelector('[data-slot="data-table-expanded"]')
      expect(el).toBeTruthy()
      const boxes = el!.querySelectorAll('input[type="checkbox"]')
      expect(boxes.length).toBeGreaterThan(0)
      return el
    })) as HTMLElement

    // Dirty the checklist so Save appears, then save.
    await u.click(panel.querySelectorAll('input[type="checkbox"]')[0])
    requestMock.mockImplementationOnce(() =>
      Promise.resolve({ success: true, capabilities: ["task.create"] }),
    )
    await u.click(within(panel).getByRole("button", { name: "Save" }))

    await screen.findByText("Saved — devs now has 1 capability")
  })
})
