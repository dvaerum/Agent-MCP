// @vitest-environment jsdom
//
// AX-2 — <CreateMemoryModal> result feedback.
//
// Feedback is delegated to the parent's shared toast (handleCreateMemory
// in memories-dashboard calls toastSuccess / toastError — both
// role="status"/"alert" + aria-live, so the outcome is announced
// accessibly). The modal's own contract is: close on success, and stay
// open on failure so the admin can retry. This test pins that contract
// (previously the catch only console.error'd).
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

// Wave 6: the modal reads existing keys from the shared `/all-data`
// TanStack Query via `useContextRows`; stub it.
vi.mock("@/lib/queries/all-data", () => ({
  useContextRows: () => [],
}))

import { CreateMemoryModal } from "@/components/dashboard/modals/create-memory-modal"

afterEach(() => cleanup())

async function openAndSubmit(onCreateMemory: () => Promise<void>) {
  const u = setupUser()
  render(<CreateMemoryModal onCreateMemory={onCreateMemory} />)
  await u.click(screen.getByRole("button", { name: /New Memory/i }))
  const key = await screen.findByLabelText(/Memory Key/i)
  await u.type(key, "api.config.base_url")
  await u.click(screen.getByRole("button", { name: /Create Memory/i }))
  return u
}

describe("CreateMemoryModal result feedback (AX-2)", () => {
  it("closes the dialog on success", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined)
    await openAndSubmit(onCreate)
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeNull(),
    )
  })

  it("keeps the dialog open on failure so the admin can retry", async () => {
    const onCreate = vi.fn().mockRejectedValue(new Error("boom"))
    await openAndSubmit(onCreate)
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
    // Dialog must still be present after the rejection.
    expect(screen.getByRole("dialog")).toBeTruthy()
  })
})
