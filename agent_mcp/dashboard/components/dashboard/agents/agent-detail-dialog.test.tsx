// @vitest-environment jsdom
//
// Unit tests for the extracted Agent detail dialog — the ~500-line
// component that carried the MCP-onboarding tabs, the token reveal, and
// the in-modal action rules inside the god-file.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  ResizeObserverStub

vi.mock("@/lib/stores/data-store", () => ({
  useDataStore: (selector?: (s: unknown) => unknown) => {
    const state = { data: null, getAgentTasks: () => [] }
    return selector ? selector(state) : state
  },
}))

import { AgentDetailDialog } from "@/components/dashboard/agents/agent-detail-dialog"
import { ACTIVE_TAB_STORAGE_KEY } from "@/lib/mcp-snippets"
import type { Agent } from "@/lib/api"

const agent = {
  agent_id: "worker-1",
  status: "created",
  created_at: "2026-01-01T00:00:00Z",
  auth_token: "0123456789abcdef0123456789abcdef",
} as unknown as Agent

beforeEach(() => window.localStorage.clear())
afterEach(() => cleanup())

describe("<AgentDetailDialog>", () => {
  it("renders nothing without an agent (the deleted-while-open case)", () => {
    const { container } = render(
      <AgentDetailDialog
        agent={null}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
      />,
    )
    expect(container.textContent).toBe("")
  })

  it("masks the bearer token until Reveal is pressed", async () => {
    render(
      <AgentDetailDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
      />,
    )
    expect(screen.getByText("...cdef")).toBeTruthy()
    await setupUser().click(screen.getByRole("button", { name: "Reveal" }))
    expect(screen.getByText(agent.auth_token!)).toBeTruthy()
  })

  it("offers a tab per supported MCP client, wired to this agent's token", () => {
    render(
      <AgentDetailDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
      />,
    )
    for (const label of [
      "Claude Code", "OpenCode", "Cursor", "Cline", "Zed",
      "Continue.dev", "Generic JSON",
    ]) {
      expect(screen.getByRole("tab", { name: label })).toBeTruthy()
    }
    // Claude Code's tab ships two blocks (CLI + JSON), both tokened.
    expect(
      screen.getAllByText(new RegExp(`Bearer ${agent.auth_token}`)).length,
    ).toBeGreaterThan(0)
  })

  it("remembers the operator's preferred client across sessions", async () => {
    const { unmount } = render(
      <AgentDetailDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
      />,
    )
    await setupUser().click(screen.getByRole("tab", { name: "Zed" }))
    await waitFor(() =>
      expect(window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY)).toBe("zed"),
    )
    unmount()

    render(
      <AgentDetailDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
      />,
    )
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "Zed" }).getAttribute("data-state"),
      ).toBe("active"),
    )
  })

  it("shows Edit/Terminate/Send-directive for a live non-Admin agent", () => {
    render(
      <AgentDetailDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
        onEdit={() => {}}
        onTerminate={() => {}}
        onPurge={() => {}}
        onSendDirective={() => {}}
      />,
    )
    expect(screen.getByRole("button", { name: /Send directive/ })).toBeTruthy()
    expect(screen.getByRole("button", { name: /Edit/ })).toBeTruthy()
    expect(screen.getByRole("button", { name: /Terminate/ })).toBeTruthy()
    expect(screen.queryByRole("button", { name: /Purge/ })).toBeNull()
  })

  it("swaps Terminate for Purge once the agent is terminated", () => {
    render(
      <AgentDetailDialog
        agent={{ ...agent, status: "terminated" } as Agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
        onEdit={() => {}}
        onTerminate={() => {}}
        onPurge={() => {}}
        onSendDirective={() => {}}
      />,
    )
    expect(screen.getByRole("button", { name: /Purge/ })).toBeTruthy()
    expect(screen.queryByRole("button", { name: /Terminate/ })).toBeNull()
    expect(screen.queryByRole("button", { name: /Send directive/ })).toBeNull()
  })

  it("hides every mutating action on the Admin pseudo-agent", () => {
    render(
      <AgentDetailDialog
        agent={{ ...agent, agent_id: "Admin" } as Agent}
        open
        onOpenChange={() => {}}
        onTaskClick={() => {}}
        onEdit={() => {}}
        onTerminate={() => {}}
        onPurge={() => {}}
        onSendDirective={() => {}}
      />,
    )
    for (const name of [/Send directive/, /^Edit$/, /Terminate/, /Purge/]) {
      expect(screen.queryByRole("button", { name })).toBeNull()
    }
  })
})
