// @vitest-environment jsdom
//
// Unit tests for the extracted Purge dialog.
//
// Purge is the Agents page's one irreversible action (hard DELETE +
// cascade tombstone). The migration routes it through the shared
// <DeleteConfirmModal> (architecture review Class 5) instead of the
// hand-rolled confirm it carried inside the god-file, so it now inherits
// the type-to-confirm gate; the blast-radius preview it used to render
// itself became that modal's `details` slot.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

const getPurgePreview = vi.fn()
const purgeAgent = vi.fn()
vi.mock("@/lib/api", () => ({
  apiClient: {
    getPurgePreview: (...a: unknown[]) => getPurgePreview(...a),
    purgeAgent: (...a: unknown[]) => purgeAgent(...a),
  },
  ApiError: class ApiError extends Error {},
}))

import { PurgeAgentDialog } from "@/components/dashboard/agents/purge-agent-dialog"

const preview = {
  agent_id: "worker-1",
  status: "terminated",
  tombstone: "[deleted-worker-1]",
  counts: {
    messages_sent: 3,
    messages_received: 4,
    tasks_created: 5,
    tasks_assigned: 6,
    agent_actions: 7,
  },
  samples: {
    messages_sent: [{ content: "hello", timestamp: "2026-01-01T00:00:00Z" }],
    tasks_created: ["build the thing"],
  },
}

afterEach(() => {
  cleanup()
  getPurgePreview.mockReset()
  purgeAgent.mockReset()
})

function renderDialog(onConfirmed = vi.fn(), onOpenChange = vi.fn()) {
  render(
    <PurgeAgentDialog
      agentId="worker-1"
      open
      onOpenChange={onOpenChange}
      onConfirmed={onConfirmed}
    />,
  )
  return { onConfirmed, onOpenChange }
}

describe("<PurgeAgentDialog>", () => {
  it("shows the blast radius from the purge preview", async () => {
    getPurgePreview.mockResolvedValue(preview)
    renderDialog()
    await waitFor(() =>
      expect(screen.getByText(/3 messages sent/)).toBeTruthy(),
    )
    expect(screen.getByText(/4 messages received/)).toBeTruthy()
    expect(screen.getByText(/5 tasks created/)).toBeTruthy()
    expect(screen.getByText(/6 tasks assigned/)).toBeTruthy()
    expect(screen.getByText(/7 agent_actions entries/)).toBeTruthy()
    expect(screen.getByText("[deleted-worker-1]")).toBeTruthy()
  })

  it("keeps the confirm button disarmed until DELETE is typed", async () => {
    getPurgePreview.mockResolvedValue(preview)
    renderDialog()
    const confirm = screen.getByRole("button", { name: /Confirm purge/ })
    expect(confirm).toHaveProperty("disabled", true)

    await setupUser().type(screen.getByLabelText(/to confirm deletion/i), "DELETE")
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false))
  })

  it("purges and notifies the page once confirmed", async () => {
    getPurgePreview.mockResolvedValue(preview)
    purgeAgent.mockResolvedValue({})
    const { onConfirmed } = renderDialog()
    await setupUser().type(screen.getByLabelText(/to confirm deletion/i), "DELETE")
    await setupUser().click(screen.getByRole("button", { name: /Confirm purge/ }))
    await waitFor(() => expect(purgeAgent).toHaveBeenCalledWith("worker-1"))
    expect(onConfirmed).toHaveBeenCalled()
  })

  it("surfaces a failed purge inline and keeps the dialog open", async () => {
    getPurgePreview.mockResolvedValue(preview)
    purgeAgent.mockRejectedValue(new Error("cascade failed"))
    const { onConfirmed, onOpenChange } = renderDialog()
    await setupUser().type(screen.getByLabelText(/to confirm deletion/i), "DELETE")
    await setupUser().click(screen.getByRole("button", { name: /Confirm purge/ }))
    await waitFor(() => expect(screen.getByText("cascade failed")).toBeTruthy())
    expect(onConfirmed).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it("shows a failed preview load inline (it is a read, not a mutation)", async () => {
    getPurgePreview.mockRejectedValue(new Error("preview exploded"))
    renderDialog()
    await waitFor(() =>
      expect(screen.getByText("preview exploded")).toBeTruthy(),
    )
  })
})
