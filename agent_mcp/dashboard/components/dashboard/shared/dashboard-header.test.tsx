// @vitest-environment jsdom
//
// Unit tests for the shared <DashboardHeader>. This header block (title
// + subtitle + project chip w/ status dot + "Last updated" + Refresh +
// action slot) was copy-pasted verbatim across memories/settings/
// messages/agents/tasks — every copy carried a `// matches …` comment.
// These tests pin the single reusable contract.
import { describe, it, expect, afterEach, vi } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"
import { setupUserPlain } from "@/tests/support/user-event"

import { DashboardHeader } from "@/components/dashboard/shared/dashboard-header"

afterEach(() => cleanup())

describe("<DashboardHeader>", () => {
  it("renders title and subtitle", () => {
    render(<DashboardHeader title="Memory Bank" subtitle="Manage context" />)
    expect(screen.getByRole("heading", { name: "Memory Bank" })).toBeTruthy()
    expect(screen.getByText("Manage context")).toBeTruthy()
  })

  it("renders the project chip only when serverName is given", () => {
    const { rerender } = render(<DashboardHeader title="T" />)
    expect(screen.queryByText("proj-1")).toBeNull()
    rerender(<DashboardHeader title="T" serverName="proj-1" />)
    expect(screen.getByText("proj-1")).toBeTruthy()
  })

  it("shows 'Last updated' only when lastUpdated is provided", () => {
    const { rerender } = render(<DashboardHeader title="T" />)
    expect(screen.queryByText(/Last updated/)).toBeNull()
    rerender(<DashboardHeader title="T" lastUpdated="2026-01-01T12:00:00Z" />)
    expect(screen.getByText(/Last updated/)).toBeTruthy()
  })

  it("renders a Refresh button that calls onRefresh", async () => {
    const u = setupUserPlain()
    const onRefresh = vi.fn()
    render(<DashboardHeader title="T" onRefresh={onRefresh} />)
    await u.click(screen.getByRole("button", { name: /refresh/i }))
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it("disables Refresh while refreshing", () => {
    render(<DashboardHeader title="T" onRefresh={() => {}} refreshing />)
    const btn = screen.getByRole("button", { name: /refresh/i }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("omits Refresh entirely when onRefresh is not given", () => {
    render(<DashboardHeader title="T" />)
    expect(screen.queryByRole("button", { name: /refresh/i })).toBeNull()
  })

  it("renders the actions slot", () => {
    render(
      <DashboardHeader title="T" actions={<button>Create memory</button>} />,
    )
    expect(screen.getByRole("button", { name: "Create memory" })).toBeTruthy()
  })
})
