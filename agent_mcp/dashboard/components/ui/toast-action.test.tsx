// @vitest-environment jsdom
//
// Toast ACTION slot — the undo affordance.
//
// Pre-PR `ToastOptions` carried only {title, description, variant,
// durationMs}, so no toast in the dashboard could offer a follow-up
// control. That absence is load-bearing: Material's confirmation
// guidance ("confirmation isn't necessary when the consequences of an
// action are reversible") is only reachable once a reversible action
// can actually OFFER the reversal, and M3's snackbar guidance names
// "Undo" as the canonical snackbar action.
//
// These tests pin the contract:
//   * an action renders as a button inside the toast,
//   * clicking it runs the handler and dismisses the toast,
//   * a REJECTING handler surfaces an error toast — the user is never
//     left believing state was restored when it wasn't,
//   * `toastUndo` is the honest wrapper the Tier-0 call sites use,
//   * no action → no button (the pre-existing toasts are unchanged).
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

import {
  Toaster,
  toast,
  toastSuccess,
  toastUndo,
  __resetToastsForTests,
} from "@/components/ui/toast"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

afterEach(() => {
  cleanup()
  __resetToastsForTests()
})

describe("toast action slot", () => {
  it("renders no action button when the toast has no action", async () => {
    render(<Toaster />)
    toastSuccess("Saved.")
    await screen.findByTestId("toast")
    expect(screen.queryByTestId("toast-action")).toBeNull()
  })

  it("renders the action as a labelled button", async () => {
    render(<Toaster />)
    toast({
      variant: "success",
      description: "Removed alice.",
      action: { label: "Undo", onAction: vi.fn() },
    })
    const btn = await screen.findByRole("button", { name: "Undo" })
    expect(btn.getAttribute("data-testid")).toBe("toast-action")
  })

  it("fires the handler and dismisses the toast on success", async () => {
    const onAction = vi.fn().mockResolvedValue(undefined)
    render(<Toaster />)
    toast({
      variant: "success",
      description: "Removed alice.",
      action: { label: "Undo", onAction },
    })
    await setupUser().click(await screen.findByTestId("toast-action"))
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByTestId("toast-action")).toBeNull())
  })

  it("surfaces an error toast when the action handler rejects", async () => {
    const onAction = vi.fn().mockRejectedValue(new Error("row is gone"))
    render(<Toaster />)
    toast({
      variant: "success",
      description: "Removed alice.",
      action: { label: "Undo", onAction },
    })
    await setupUser().click(await screen.findByTestId("toast-action"))
    await waitFor(() => expect(onAction).toHaveBeenCalled())
    // The failure must reach the user — an error-variant toast quoting
    // the server/client message.
    const err = await screen.findByText(/row is gone/)
    expect(err).toBeTruthy()
    const toasts = await screen.findAllByTestId("toast")
    expect(toasts.some((t) => t.getAttribute("data-variant") === "error")).toBe(
      true,
    )
  })

  it("ignores repeat clicks while the action is in flight", async () => {
    let resolve!: () => void
    const onAction = vi.fn(
      () => new Promise<void>((r) => { resolve = () => r() }),
    )
    render(<Toaster />)
    toast({
      variant: "success",
      description: "Removed alice.",
      action: { label: "Undo", onAction },
    })
    const user = setupUser()
    const btn = await screen.findByTestId("toast-action")
    await user.click(btn)
    await user.click(btn)
    expect(onAction).toHaveBeenCalledTimes(1)
    resolve()
  })

  it("gives action toasts a longer default dwell than a plain toast", async () => {
    render(<Toaster />)
    const plain = toastSuccess("Saved.")
    const withAction = toast({
      variant: "success",
      description: "Removed alice.",
      action: { label: "Undo", onAction: vi.fn() },
    })
    expect(withAction).not.toBe(plain)
    // Both ids exist; the dwell difference is asserted through the
    // exported defaults so the test doesn't need fake timers.
    const { ACTION_TOAST_DURATION_MS, DEFAULT_TOAST_DURATION_MS } = await import(
      "@/components/ui/toast"
    )
    expect(ACTION_TOAST_DURATION_MS).toBeGreaterThan(DEFAULT_TOAST_DURATION_MS)
  })
})

describe("toastUndo", () => {
  it("shows a success toast carrying an Undo action", async () => {
    render(<Toaster />)
    toastUndo("Removed alice from devs.", vi.fn().mockResolvedValue(undefined))
    expect(await screen.findByText("Removed alice from devs.")).toBeTruthy()
    const t = await screen.findByTestId("toast")
    expect(t.getAttribute("data-variant")).toBe("success")
    expect(await screen.findByRole("button", { name: "Undo" })).toBeTruthy()
  })

  it("confirms the restore with its own toast when undo succeeds", async () => {
    const undo = vi.fn().mockResolvedValue(undefined)
    render(<Toaster />)
    toastUndo("Removed alice from devs.", undo, {
      undoneMessage: "alice restored to devs.",
    })
    await setupUser().click(await screen.findByTestId("toast-action"))
    await waitFor(() => expect(undo).toHaveBeenCalled())
    expect(await screen.findByText("alice restored to devs.")).toBeTruthy()
  })

  it("tells the user when the undo call itself fails", async () => {
    const undo = vi.fn().mockRejectedValue(new Error("409 already exists"))
    render(<Toaster />)
    toastUndo("Removed alice from devs.", undo)
    await setupUser().click(await screen.findByTestId("toast-action"))
    await waitFor(() => expect(undo).toHaveBeenCalled())
    expect(await screen.findByText(/409 already exists/)).toBeTruthy()
    // …and NOT a "restored" confirmation.
    expect(screen.queryByText(/restored/i)).toBeNull()
  })
})

describe("the live region survives an open modal dialog", () => {
  // Radix's modal Dialog runs `hideOthers(content)` (the `aria-hidden`
  // package) on mount, which stamps `aria-hidden="true"` on every
  // sibling of the portalled content. An Undo button inside an
  // aria-hidden subtree is invisible to a screen reader — i.e. the
  // affordance this whole PR adds would not exist for the users the
  // APG work is FOR.
  //
  // `hideOthers` deliberately spares `[aria-live]` elements, so the
  // Toaster escapes IF its live region is already in the DOM when the
  // dialog opens. Rendering `null` while idle made that a coin flip.
  // The region is now always mounted, which is also the correct live-
  // region idiom: assistive tech is meant to observe an existing
  // region, not discover one that appears with its first message.
  it("keeps toasts out of the aria-hidden subtree", async () => {
    render(<Toaster />)
    render(
      <Dialog open>
        <DialogContent className="w-[calc(100vw-2rem)]">
          <DialogHeader>
            <DialogTitle>Memberships</DialogTitle>
            <DialogDescription>Who can see this.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    toastUndo("Removed alice.", vi.fn().mockResolvedValue(undefined))
    // getByRole consults the accessibility tree, so this fails outright
    // if any ancestor carries aria-hidden.
    expect(await screen.findByRole("button", { name: "Undo" })).toBeTruthy()
  })
})

describe("toast action geometry (mobile)", () => {
  it("keeps the action inside the toast's own column, not a 4th inline cell", async () => {
    // jsdom has no layout engine, so this pins the LAYOUT CONTRACT
    // rather than measured pixels: the action button lives inside the
    // same min-w-0 text column as the description. A 4th inline flex
    // child next to icon + text + close is what overflows a 390px
    // phone once the label and the description are both long.
    render(<Toaster />)
    toast({
      variant: "success",
      description:
        "Removed a member with a rather long descriptive sentence attached.",
      action: { label: "Undo", onAction: vi.fn() },
    })
    const btn = await screen.findByTestId("toast-action")
    const column = btn.closest("[data-testid='toast-body']")
    expect(column).not.toBeNull()
    expect(column!.textContent).toContain("Removed a member")
  })
})
