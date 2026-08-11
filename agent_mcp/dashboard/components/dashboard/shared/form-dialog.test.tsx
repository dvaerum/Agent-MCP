// @vitest-environment jsdom
//
// Contract for the shared <FormDialog> shell (Wave 5, CD-2):
//   * a11y — the dialog has an accessible NAME (title) and DESCRIPTION
//     (wired via aria-describedby), the two things Radix warns about and
//     screen readers need.
//   * the footer exposes Cancel + Submit; Cancel closes, Submit runs the
//     mutation and (on success) closes; the submit gate + loading label
//     behave.
import * as React from "react"
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

vi.mock("@/components/ui/toast", () => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

import { FormDialog } from "@/components/dashboard/shared/form-dialog"

afterEach(() => cleanup())

function renderDialog(props: Partial<React.ComponentProps<typeof FormDialog>> = {}) {
  const onOpenChange = vi.fn()
  const onSubmit = props.onSubmit ?? vi.fn(async () => {})
  render(
    <FormDialog
      open
      onOpenChange={onOpenChange}
      title="Compose message"
      description="Send a message to an agent."
      onSubmit={onSubmit}
      {...props}
    >
      <label htmlFor="field">Field</label>
      <input id="field" />
    </FormDialog>,
  )
  return { onOpenChange, onSubmit }
}

describe("<FormDialog> a11y", () => {
  it("exposes an accessible name from the title", () => {
    renderDialog()
    expect(
      screen.getByRole("dialog", { name: /Compose message/ }),
    ).toBeTruthy()
  })

  it("wires the description via aria-describedby", () => {
    renderDialog()
    const dialog = screen.getByRole("dialog")
    const describedby = dialog.getAttribute("aria-describedby")
    expect(describedby).toBeTruthy()
    const desc = document.getElementById(describedby!)
    expect(desc?.textContent).toContain("Send a message to an agent.")
  })

  it("clears aria-describedby when no description is given", () => {
    render(
      <FormDialog open onOpenChange={() => {}} title="No desc" onSubmit={async () => {}}>
        <span>body</span>
      </FormDialog>,
    )
    // No description → aria-describedby must not point at a missing node.
    expect(screen.getByRole("dialog").getAttribute("aria-describedby")).toBeNull()
  })
})

describe("<FormDialog> footer", () => {
  it("renders Cancel + Submit with the given labels", () => {
    renderDialog({ submitLabel: "Send", cancelLabel: "Cancel" })
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy()
  })

  it("Cancel closes without submitting", async () => {
    const { onOpenChange, onSubmit } = renderDialog()
    await setupUser().click(screen.getByRole("button", { name: "Cancel" }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("Submit runs the mutation and closes on success", async () => {
    const onSubmit = vi.fn(async () => {})
    const { onOpenChange } = renderDialog({ onSubmit, submitLabel: "Send" })
    await setupUser().click(screen.getByRole("button", { name: "Send" }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("disables Submit when submitDisabled is set", () => {
    renderDialog({ submitDisabled: true, submitLabel: "Send" })
    expect(
      (screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it("keeps the dialog open when the mutation throws", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("boom")
    })
    const { onOpenChange } = renderDialog({
      onSubmit,
      submitLabel: "Send",
      errorMessage: "failed",
    })
    await setupUser().click(screen.getByRole("button", { name: "Send" }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onOpenChange).not.toHaveBeenCalled()
  })
})
