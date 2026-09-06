// @vitest-environment jsdom
//
// Live dashboard bug hunt (Firefox-MCP verify-all pass, 2026-09-06) —
// the group-capabilities picker rendered ALL 29 known capabilities as
// togglable checkboxes, including resource-tier ones (e.g.
// `agents.view`, `tasks.create`). Checking one of those and saving
// produced a live 400 `resource_capability_not_delegable_to_group`:
// `group_capability` rows have no `project_name` column (SEC R2-F3,
// rust/conexus-router/src/admin_group_capabilities.rs), so the PUT
// endpoint only ever accepts `system.*` capabilities for a group-level
// grant. The picker must not offer a choice the server will reject.
import { describe, it, expect, afterEach } from "vitest"
import React from "react"
import { render, cleanup, waitFor } from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { vi } from "vitest"
import { queryClient } from "@/lib/query-client"
import { routerApi } from "@/lib/router-api"
import { GroupCapabilitiesSection } from "@/components/dashboard/groups/group-capabilities-section"

vi.mock("@/components/ui/toast", () => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}))

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("group-capabilities picker only offers group-delegable (system.*) capabilities", () => {
  it("renders no checkbox for a resource-tier capability", async () => {
    queryClient.clear()
    vi.spyOn(routerApi, "request").mockResolvedValue({ capabilities: [] })

    const { container } = render(
      <GroupCapabilitiesSection groupId="g1" groupName="g1" />,
      { wrapper },
    )

    await waitFor(() =>
      expect(container.querySelector("code")).not.toBeNull(),
    )

    const codes = [...container.querySelectorAll("code")].map(
      (c) => c.textContent,
    )
    // None of these resource-tier caps (rejected server-side with 400
    // resource_capability_not_delegable_to_group) may appear.
    for (const cap of ["mcp.connect", "agents.view", "tasks.create", "memories.view"]) {
      expect(codes).not.toContain(cap)
    }
    // Every rendered checkbox must be a system.* capability.
    for (const code of codes) {
      expect(code).toMatch(/^system\./)
    }
    // The real group-delegable set must still all be present.
    expect(codes).toEqual(
      expect.arrayContaining([
        "system.view",
        "system.config.write",
        "system.users.manage",
        "system.groups.manage",
        "system.groups.capabilities.manage",
        "system.projects.manage",
        "system.sso.configure",
      ]),
    )
  })
})
