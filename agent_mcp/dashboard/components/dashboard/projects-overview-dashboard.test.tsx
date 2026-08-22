// @vitest-environment jsdom
//
// R12-F1 class-sweep miss: PR #691 added a logout control to the
// shared `<Header>` (components/layout/header.tsx), which covers every
// per-project dashboard page via `<MainLayout>`. But the cross-project
// overview at the bare `/agent-mcp/app/` root (no project segment)
// renders `<ProjectsOverviewDashboard>` directly — `app/page.tsx`'s
// `isOverview` branch skips `<MainLayout>`/`<Header>` entirely because
// this page has its own header block. That left the overview with no
// logout affordance at all, even though it's reachable pre-project-pick
// and exposes the Users/Groups/SSO/Setup tabs. This pins a logout
// control in that page's own header row, reusing the same
// `logoutUrl()`/`loginUrl()` helpers as `<Header>` (no second
// implementation).
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen, fireEvent, waitFor } from "@testing-library/react"

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
import { loginUrl, logoutUrl } from "@/lib/urls"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

let locationAssign: ReturnType<typeof vi.fn>

beforeEach(() => {
  projectsState.envelope = null
  projectsState.loading = false
  projectsState.error = null
  projectsState.fetchOverview = vi.fn()
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
  // jsdom doesn't implement real navigation; stub `assign` on every
  // test so a click doesn't spam "Not implemented: navigation".
  locationAssign = vi.fn()
  vi.stubGlobal("location", { ...window.location, assign: locationAssign })
})

describe("<ProjectsOverviewDashboard> logout control", () => {
  it("renders a logout control", () => {
    render(<ProjectsOverviewDashboard />)
    expect(
      screen.getByRole("button", { name: /log ?out/i }),
    ).toBeTruthy()
  })

  it("POSTs to /agent-mcp/logout with the session cookie on click", async () => {
    render(<ProjectsOverviewDashboard />)
    fireEvent.click(screen.getByRole("button", { name: /log ?out/i }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [url, options] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe(logoutUrl())
    expect(options).toMatchObject({ method: "POST", credentials: "include" })
  })

  it("redirects to the login page after logging out", async () => {
    render(<ProjectsOverviewDashboard />)
    fireEvent.click(screen.getByRole("button", { name: /log ?out/i }))

    await waitFor(() =>
      expect(locationAssign).toHaveBeenCalledWith(loginUrl()),
    )
  })
})
