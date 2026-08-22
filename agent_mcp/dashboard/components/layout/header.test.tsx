// @vitest-environment jsdom
//
// R12-F1: the dashboard SPA had no logout/session-termination UI
// anywhere. `<Header>` is the single shared header rendered on every
// authenticated page (via `<MainLayout>`), so the logout control lives
// here. The server-side `POST /agent-mcp/logout` route was already
// correct (POST-only, CSRF-safe via SameSite cookie, httpOnly session,
// 30-day expiry) — this only closes the missing client-side entry
// point. See lib/urls.ts `logoutUrl()`/`loginUrl()` for the mount-
// derived paths (ADR-0020: root vs tailnet front doors).
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

import { SidebarProvider } from "@/components/ui/sidebar"
import { Header } from "@/components/layout/header"
import { loginUrl, logoutUrl } from "@/lib/urls"

function renderHeader() {
  return render(
    <SidebarProvider>
      <Header />
    </SidebarProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

let locationAssign: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
  // jsdom doesn't implement real navigation; stub `assign` on every
  // test (not just the redirect-assertion one) so a click in any test
  // doesn't spam "Not implemented: navigation" to stderr.
  locationAssign = vi.fn()
  vi.stubGlobal("location", { ...window.location, assign: locationAssign })
})

describe("<Header> logout control", () => {
  it("renders a logout control", () => {
    renderHeader()
    expect(
      screen.getByRole("button", { name: /log ?out/i }),
    ).toBeTruthy()
  })

  it("POSTs to /agent-mcp/logout with the session cookie on click", async () => {
    renderHeader()
    fireEvent.click(screen.getByRole("button", { name: /log ?out/i }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [url, options] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe(logoutUrl())
    expect(options).toMatchObject({ method: "POST", credentials: "include" })
  })

  it("redirects to the login page after logging out", async () => {
    renderHeader()
    fireEvent.click(screen.getByRole("button", { name: /log ?out/i }))

    await waitFor(() =>
      expect(locationAssign).toHaveBeenCalledWith(loginUrl()),
    )
  })
})
