// @vitest-environment jsdom
//
// Unit tests for the extracted Terminate confirm dialog. Pre-split this
// component lived inside `agents-dashboard.tsx` and had no seam at all —
// exercising it meant rendering the whole page (store, api client, five
// sibling dialogs). The extraction is what makes these assertions
// possible.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { TerminateAgentDialog } from "@/components/dashboard/agents/terminate-agent-dialog"

// Radix Dialog locks `pointer-events: none` on <body> while open.
const ue = () => userEvent.setup({ pointerEventsCheck: 0 })

afterEach(() => cleanup())

describe("<TerminateAgentDialog>", () => {
  it("names the target agent and explains that terminate is reversible", () => {
    render(
      <TerminateAgentDialog
        agentId="worker-1"
        open
        onOpenChange={() => {}}
        onConfirmed={() => {}}
      />,
    )
    expect(screen.getByText(/Terminate agent worker-1\?/)).toBeTruthy()
    expect(screen.getByText(/soft-delete/i)).toBeTruthy()
  })

  it("confirms with the agent id and closes on success", async () => {
    const onConfirmed = vi.fn().mockResolvedValue(undefined)
    const onOpenChange = vi.fn()
    render(
      <TerminateAgentDialog
        agentId="worker-1"
        open
        onOpenChange={onOpenChange}
        onConfirmed={onConfirmed}
      />,
    )
    await ue().click(screen.getByRole("button", { name: "Terminate" }))
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith("worker-1"))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("stays open when the mutation rejects", async () => {
    const onConfirmed = vi.fn().mockRejectedValue(new Error("boom"))
    const onOpenChange = vi.fn()
    render(
      <TerminateAgentDialog
        agentId="worker-1"
        open
        onOpenChange={onOpenChange}
        onConfirmed={onConfirmed}
      />,
    )
    await ue().click(screen.getByRole("button", { name: "Terminate" }))
    await waitFor(() => expect(onConfirmed).toHaveBeenCalled())
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it("is a no-op when no agent is targeted", async () => {
    const onConfirmed = vi.fn()
    render(
      <TerminateAgentDialog
        agentId={null}
        open
        onOpenChange={() => {}}
        onConfirmed={onConfirmed}
      />,
    )
    await ue().click(screen.getByRole("button", { name: "Terminate" }))
    expect(onConfirmed).not.toHaveBeenCalled()
  })
})
