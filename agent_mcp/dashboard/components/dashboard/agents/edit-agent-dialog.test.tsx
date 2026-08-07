// @vitest-environment jsdom
//
// Unit tests for the extracted Edit-agent dialog.
//
// The dialog owns the form (seeding, diffing, validation); the caller
// owns the mutation via `onSave`. That split is the seam the extraction
// created — pre-split the component called `apiClient.editAgent` itself
// from inside the god-file and surfaced failures through its own
// `setError` banner, so neither half could be tested in isolation.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

// jsdom ships no ResizeObserver; Radix's Switch/Select measure their
// trigger on mount. Minimal stub so the form can render.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  ResizeObserverStub

// The dialog reads the global auto-event-loop flag out of the data
// store; stub it so the form renders without the app's store wiring.
vi.mock("@/lib/stores/data-store", () => ({
  useDataStore: (selector?: (s: unknown) => unknown) => {
    const state = { data: null }
    return selector ? selector(state) : state
  },
}))

import {
  EditAgentDialog,
  coerceAutoEventLoop,
} from "@/components/dashboard/agents/edit-agent-dialog"
import type { Agent } from "@/lib/api"

afterEach(() => cleanup())

const agent = {
  agent_id: "worker-1",
  status: "created",
  created_at: "2026-01-01T00:00:00Z",
  color: "#111111",
  working_directory: "/w",
  profile: "hello",
  agent_role: "worker",
  auto_event_loop: true,
} as unknown as Agent

describe("coerceAutoEventLoop", () => {
  // SQLite BOOLEAN arrives as 0/1 through the JSON round-trip; the
  // migration's DEFAULT 1 backfill means "missing" must read as true.
  it.each([
    [true, true],
    [false, false],
    [1, true],
    [0, false],
    ["true", true],
    ["false", false],
    ["1", true],
    ["0", false],
    [undefined, true],
    [null, true],
  ])("coerces %p to %p", (raw, expected) => {
    expect(coerceAutoEventLoop(raw)).toBe(expected)
  })
})

describe("<EditAgentDialog>", () => {
  it("ships only the fields the operator actually changed", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(
      <EditAgentDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onSave={onSave}
      />,
    )
    const profile = screen.getByLabelText(/Self-description/i)
    await setupUser().clear(profile)
    await setupUser().type(profile, "new profile")
    await setupUser().click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave).toHaveBeenCalledWith("worker-1", { profile: "new profile" })
  })

  it("closes without calling onSave when nothing changed", async () => {
    const onSave = vi.fn()
    const onOpenChange = vi.fn()
    render(
      <EditAgentDialog
        agent={agent}
        open
        onOpenChange={onOpenChange}
        onSave={onSave}
      />,
    )
    await setupUser().click(screen.getByRole("button", { name: "Save changes" }))
    expect(onSave).not.toHaveBeenCalled()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("keeps the dialog open (edits intact) when the mutation rejects", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("nope"))
    const onOpenChange = vi.fn()
    render(
      <EditAgentDialog
        agent={agent}
        open
        onOpenChange={onOpenChange}
        onSave={onSave}
      />,
    )
    const profile = screen.getByLabelText(/Self-description/i)
    await setupUser().clear(profile)
    await setupUser().type(profile, "x")
    await setupUser().click(screen.getByRole("button", { name: "Save changes" }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    expect((profile as HTMLTextAreaElement).value).toBe("x")
  })

  it("disarms Save while the AoE session id is malformed", async () => {
    const onSave = vi.fn()
    render(
      <EditAgentDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onSave={onSave}
      />,
    )
    const aoe = screen.getByPlaceholderText(/16-char lowercase hex/i)
    await setupUser().type(aoe, "nothex")
    expect(
      screen.getByRole("button", { name: "Save changes" }),
    ).toHaveProperty("disabled", true)
    expect(screen.getByText(/Must be 16 lowercase hex chars/i)).toBeTruthy()
  })

  it("accepts a well-formed AoE session id and ships it lowercased", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(
      <EditAgentDialog
        agent={agent}
        open
        onOpenChange={() => {}}
        onSave={onSave}
      />,
    )
    await setupUser().type(
      screen.getByPlaceholderText(/16-char lowercase hex/i),
      "551e7a79d11f435b",
    )
    await setupUser().click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave).toHaveBeenCalledWith("worker-1", {
      aoe_session_id: "551e7a79d11f435b",
    })
  })
})
