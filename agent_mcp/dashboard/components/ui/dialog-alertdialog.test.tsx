// @vitest-environment jsdom
//
// `role="alertdialog"` opt-in on the shared <DialogContent>.
//
// W3C ARIA APG names confirmation prompts as the canonical alertdialog
// case: the role "enables assistive technologies and browsers to
// distinguish alert dialogs from other dialogs". Pre-PR
// `components/ui/dialog.tsx` never set it, so every confirm in the
// dashboard announced as a plain dialog.
//
// Implemented as an opt-in PROP rather than a separate
// <AlertDialogContent> export on purpose: `tests/
// test_dashboard_polish_mobile_pass.py` globs the component tree for
// `<DialogContent … className="…">` to audit the mobile-width
// fallback. A parallel component name would silently drop every
// migrated confirm out of that audit.
import { describe, it, expect } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"
import { afterEach } from "vitest"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

afterEach(() => cleanup())

describe("<DialogContent alertDialog>", () => {
  it("defaults to role=dialog", () => {
    render(
      <Dialog open>
        <DialogContent className="w-[calc(100vw-2rem)]">
          <DialogHeader>
            <DialogTitle>Edit thing</DialogTitle>
            <DialogDescription>Change the thing.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    expect(screen.getByRole("dialog")).toBeTruthy()
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })

  it("opts in to role=alertdialog", () => {
    render(
      <Dialog open>
        <DialogContent alertDialog className="w-[calc(100vw-2rem)]">
          <DialogHeader>
            <DialogTitle>Delete thing</DialogTitle>
            <DialogDescription>This cannot be undone.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("does not leak the prop onto the DOM node", () => {
    render(
      <Dialog open>
        <DialogContent alertDialog className="w-[calc(100vw-2rem)]">
          <DialogHeader>
            <DialogTitle>Delete thing</DialogTitle>
            <DialogDescription>This cannot be undone.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    const node = screen.getByRole("alertdialog")
    expect(node.hasAttribute("alertdialog")).toBe(false)
    expect(node.hasAttribute("alertDialog")).toBe(false)
  })

  it("keeps Radix's aria-describedby wiring to <DialogDescription>", () => {
    render(
      <Dialog open>
        <DialogContent alertDialog className="w-[calc(100vw-2rem)]">
          <DialogHeader>
            <DialogTitle>Delete thing</DialogTitle>
            <DialogDescription>
              The thing is gone forever.
            </DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    const node = screen.getByRole("alertdialog")
    const describedBy = node.getAttribute("aria-describedby")
    expect(describedBy).toBeTruthy()
    const described = describedBy!
      .split(/\s+/)
      .map((id) => document.getElementById(id))
    expect(described.every((el) => el !== null)).toBe(true)
    expect(described.map((el) => el!.textContent).join(" ")).toContain(
      "gone forever",
    )
  })

  it("lets a caller point aria-describedby at its own warning text", () => {
    render(
      <Dialog open>
        <DialogContent
          alertDialog
          aria-describedby="warn-id"
          className="w-[calc(100vw-2rem)]"
        >
          <DialogHeader>
            <DialogTitle>Delete thing</DialogTitle>
            <DialogDescription>Short summary.</DialogDescription>
          </DialogHeader>
          <div id="warn-id">Permanent data loss. This cannot be reversed.</div>
        </DialogContent>
      </Dialog>,
    )
    const node = screen.getByRole("alertdialog")
    expect(node.getAttribute("aria-describedby")).toBe("warn-id")
    expect(document.getElementById("warn-id")!.textContent).toContain(
      "cannot be reversed",
    )
  })
})
