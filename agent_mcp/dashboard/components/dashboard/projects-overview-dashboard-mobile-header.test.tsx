// @vitest-environment jsdom
//
// Live dashboard bug hunt (Firefox-MCP verify-all pass, 2026-09-06) —
// at a 390px mobile viewport the root `/agent-mcp/app/` overview's
// header row ("Agent MCP — Projects" + Add project/Refresh/Log out
// buttons) used a plain `flex items-center justify-between` with no
// responsive stacking. The three fixed-width buttons never shrink, so
// the title column is squeezed narrow enough to wrap onto 3 lines;
// `items-center` then vertically centers that multi-line title against
// the single-line button row, visually overlapping the wrapped title
// text underneath the "Add project" button. Every other page header in
// this dashboard (overview-dashboard.tsx, settings-dashboard.tsx,
// prompt-book-dashboard.tsx) already uses
// `flex-col sm:flex-row sm:items-center sm:justify-between` to stack
// vertically below the `sm` breakpoint instead — this page was the one
// header missing that established pattern.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"

const projectsState = {
  envelope: null as unknown,
  loading: false,
  error: null as string | null,
  fetchOverview: vi.fn(),
}

vi.mock("@/lib/stores/projects-store", () => ({
  useProjectsStore: () => projectsState,
}))

import { ProjectsOverviewDashboard } from "@/components/dashboard/projects-overview-dashboard"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

beforeEach(() => {
  projectsState.envelope = null
  projectsState.loading = false
  projectsState.error = null
  projectsState.fetchOverview = vi.fn()
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
})

describe("<ProjectsOverviewDashboard> header — mobile stacking", () => {
  it("stacks the title above the button row below the sm breakpoint instead of overlapping", () => {
    render(<ProjectsOverviewDashboard />)
    const heading = screen.getByRole("heading", { name: /Agent MCP — Projects/ })
    // Same header row that contains both the title block and the
    // Add project/Refresh/Log out button row (their nearest common
    // flex container).
    const headerRow = heading.closest("div")!.parentElement!
    expect(headerRow.className).toContain("flex-col")
    expect(headerRow.className).toContain("sm:flex-row")
  })
})
