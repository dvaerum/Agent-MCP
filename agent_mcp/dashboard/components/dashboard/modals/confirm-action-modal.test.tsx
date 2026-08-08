// @vitest-environment jsdom
//
// Tier-1 confirmation modal — the shared simple-confirm.
//
// RED before `confirm-action-modal.tsx` existed. Tasks, Schedules,
// Terminate and (post-downgrade) Memories each hand-rolled the same
// {busy, error} + Cancel/destructive-confirm dialog; this pins the one
// contract they now share.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

import { ConfirmActionModal } from "@/components/dashboard/modals/confirm-action-modal"

afterEach(() => cleanup())

describe("<ConfirmActionModal>", () => {
  it("fires onConfirm on a single click — no type-to-confirm gate", async () => {
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        title="Delete task"
        description="Delete task “x”? This cannot be undone."
      />,
    )
    // Tier 1 has NO confirmation text field at all — that is the whole
    // point of the tier (see the modal's habituation rationale).
    expect(document.querySelector("input")).toBeNull()

    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  it("names the target in the dialog copy", () => {
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={() => Promise.resolve()}
        title="Delete task"
        description="Delete task “Ship the thing”? This cannot be undone."
      />,
    )
    expect(screen.getByText(/Ship the thing/)).toBeTruthy()
  })

  it("closes on success", async () => {
    const onOpenChange = vi.fn()
    render(
      <ConfirmActionModal
        open
        onOpenChange={onOpenChange}
        onConfirm={() => Promise.resolve()}
        title="Delete schedule"
      />,
    )
    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it("shows a failure inline and keeps the dialog open", async () => {
    const onOpenChange = vi.fn()
    render(
      <ConfirmActionModal
        open
        onOpenChange={onOpenChange}
        onConfirm={() => Promise.reject(new Error("boom"))}
        title="Delete task"
      />,
    )
    await setupUser().click(screen.getByRole("button", { name: /^Delete$/ }))
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy())
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it("renders the details slot (the recreatable-value preview)", () => {
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={() => Promise.resolve()}
        title="Delete memory"
        details={<div>memory.value.preview</div>}
      />,
    )
    expect(screen.getByText("memory.value.preview")).toBeTruthy()
  })

  it("honours confirmLabel / busyLabel / confirmTestId overrides", async () => {
    let release: () => void = () => {}
    const pending = new Promise<void>((r) => {
      release = r
    })
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={() => pending}
        title="Terminate agent"
        confirmLabel="Terminate"
        busyLabel="Terminating..."
        confirmTestId="confirm-terminate-btn"
      />,
    )
    const btn = screen.getByTestId("confirm-terminate-btn")
    expect(btn.textContent).toContain("Terminate")
    await setupUser().click(btn)
    await waitFor(() => expect(btn.textContent).toContain("Terminating..."))
    expect(btn).toHaveProperty("disabled", true)
    release()
  })

  it("puts Cancel before the destructive confirm (Cancel-left)", () => {
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={() => Promise.resolve()}
        title="Delete task"
      />,
    )
    const buttons = screen.getAllByRole("button")
    const labels = buttons.map((b) => b.textContent ?? "")
    const cancel = labels.findIndex((t) => t.includes("Cancel"))
    const confirm = labels.findIndex((t) => t.includes("Delete"))
    expect(cancel).toBeGreaterThanOrEqual(0)
    expect(cancel).toBeLessThan(confirm)
  })
})
