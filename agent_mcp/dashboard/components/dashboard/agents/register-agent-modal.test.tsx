// @vitest-environment jsdom
//
// Unit tests for the extracted Register-agent modal.
//
// Wave 7 made this the sole agent-creation surface; the 2026-06-17
// Firefox-MCP click-through bug it was built to fix ("server 400s, the
// dialog silently closes, the operator's input is gone") is exactly the
// property asserted below — and, pre-extraction, could only be checked
// by rendering the whole Agents page.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

// jsdom ships no ResizeObserver; Radix's Select measures its trigger.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  ResizeObserverStub

const registerAgent = vi.fn()
vi.mock("@/lib/api", () => ({
  apiClient: { registerAgent: (...a: unknown[]) => registerAgent(...a) },
  ApiError: class ApiError extends Error {},
}))

import {
  AGENT_ID_RE,
  RegisterAgentModal,
} from "@/components/dashboard/agents/register-agent-modal"

afterEach(() => {
  cleanup()
  registerAgent.mockReset()
})

describe("AGENT_ID_RE", () => {
  // Mirrors the backend's `_AGENT_ID_RE` in agent_repository.py — the
  // live hint exists so the operator learns before eating a 400.
  it.each(["a", "worker-1", "worker@host", "pikvm_mcp_server@host"])(
    "accepts %s",
    (id) => expect(AGENT_ID_RE.test(id)).toBe(true),
  )
  it.each(["Worker", "1worker", "-worker", "worker-", "bad name", "worker!"])(
    "rejects %s",
    (id) => expect(AGENT_ID_RE.test(id)).toBe(false),
  )
})

async function openModal() {
  render(<RegisterAgentModal />)
  await setupUser().click(screen.getByRole("button", { name: /Register Agent/ }))
  return screen.getByPlaceholderText("worker-analytics-01")
}

describe("<RegisterAgentModal>", () => {
  it("disarms Register and explains the slug rule for an invalid id", async () => {
    const input = await openModal()
    await setupUser().type(input, "Bad Name!")
    expect(screen.getByRole("button", { name: "Register" })).toHaveProperty(
      "disabled",
      true,
    )
    expect(screen.getByText(/Lowercase slug only/)).toBeTruthy()
  })

  it("hands back the paste-ready snippet on success", async () => {
    registerAgent.mockResolvedValue({
      agent_id: "worker-1",
      agent_token: "tok",
      mcp_snippet: '{"mcpServers": {"agent-mcp": {}}}',
      message: "ok",
    })
    const input = await openModal()
    await setupUser().type(input, "worker-1")
    await setupUser().click(screen.getByRole("button", { name: "Register" }))

    await waitFor(() => expect(screen.getByText("Agent registered")).toBeTruthy())
    expect(screen.getByText('{"mcpServers": {"agent-mcp": {}}}')).toBeTruthy()
    expect(screen.getByRole("button", { name: /Copy snippet/ })).toBeTruthy()
  })

  it("keeps the dialog open with the typed id when the server rejects", async () => {
    registerAgent.mockRejectedValue(new Error("invalid agent_id"))
    const input = await openModal()
    await setupUser().type(input, "worker-1")
    await setupUser().click(screen.getByRole("button", { name: "Register" }))

    await waitFor(() => expect(registerAgent).toHaveBeenCalled())
    // Still on pane 1, input preserved — the 2026-06-17 regression.
    expect(screen.queryByText("Agent registered")).toBeNull()
    expect((input as HTMLInputElement).value).toBe("worker-1")
  })

  it("treats a response missing the token/snippet as a failure", async () => {
    registerAgent.mockResolvedValue({ agent_id: "worker-1" })
    const input = await openModal()
    await setupUser().type(input, "worker-1")
    await setupUser().click(screen.getByRole("button", { name: "Register" }))

    await waitFor(() => expect(registerAgent).toHaveBeenCalled())
    expect(screen.queryByText("Agent registered")).toBeNull()
  })

  it("defaults the role to worker and forwards it to the backend", async () => {
    registerAgent.mockResolvedValue({
      agent_id: "worker-1",
      agent_token: "tok",
      mcp_snippet: "{}",
      message: "ok",
    })
    const input = await openModal()
    await setupUser().type(input, "worker-1")
    await setupUser().click(screen.getByRole("button", { name: "Register" }))
    await waitFor(() => expect(registerAgent).toHaveBeenCalled())
    expect(registerAgent.mock.calls[0]![0]).toMatchObject({
      name: "worker-1",
      role: "worker",
    })
  })
})
