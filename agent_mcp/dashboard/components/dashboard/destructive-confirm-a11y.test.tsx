// @vitest-environment jsdom
//
// Accessibility contract for every destructive confirmation dialog.
//
// Two W3C ARIA APG requirements, pinned per dialog:
//
//   1. role="alertdialog" — the APG names confirmation prompts as the
//      canonical case; the role "enables assistive technologies and
//      browsers to distinguish alert dialogs from other dialogs".
//
//   2. Initial focus must NOT land on the destructive control. The APG
//      dialog-modal pattern: "If a dialog contains the final step in a
//      process that is not easily reversible… it may be advisable to
//      set focus on the least destructive action."
//
//      Radix's FocusScope already autofocuses the FIRST TABBABLE
//      element in DOM order (`focusFirst(removeLinks(
//      getTabbableCandidates(container)))` in
//      @radix-ui/react-focus-scope). Every dialog below happens to put
//      a non-destructive control first — Cancel, a checkbox, or a
//      type-to-confirm input whose destructive button is disabled
//      until it is satisfied — so no extra focus code is warranted.
//      These tests pin that so a future footer reorder (or a migration
//      that moves the destructive button up) can't silently regress it.
//
//   3. The accessible DESCRIPTION must carry the consequence, not just
//      the title.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"

const routerRequest = vi.fn().mockResolvedValue({})
vi.mock("@/lib/router-api", () => ({
  routerApi: { request: (...a: unknown[]) => routerRequest(...a) },
}))
const fetchOverview = vi.fn().mockResolvedValue(undefined)
vi.mock("@/lib/stores/projects-store", () => ({
  useProjectsStore: (sel: (s: unknown) => unknown) =>
    sel({ fetchOverview }),
}))

import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"
import { ConfirmActionModal } from "@/components/dashboard/modals/confirm-action-modal"
import { TerminateAgentDialog } from "@/components/dashboard/agents/terminate-agent-dialog"
import { RemoveProjectModal } from "@/components/dashboard/remove-project-modal"
import { AliasChipPanel } from "@/components/dashboard/alias-chip-panel"

afterEach(() => {
  cleanup()
  routerRequest.mockClear()
})

/** Resolve an element's accessible description from aria-describedby. */
function describedText(node: HTMLElement): string {
  const ids = (node.getAttribute("aria-describedby") ?? "").split(/\s+/)
  return ids
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ")
}

describe("<DeleteConfirmModal> (users, groups, memories, messages, purge)", () => {
  const renderModal = () =>
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={async () => {}}
        entityLabel="Memory"
      />,
    )

  it("announces as an alertdialog", () => {
    renderModal()
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("describes the CONSEQUENCE, not just the title", () => {
    renderModal()
    expect(describedText(screen.getByRole("alertdialog"))).toMatch(
      /permanently removed|cannot be reversed/i,
    )
  })

  it("does not focus the destructive button on open", async () => {
    renderModal()
    const destructive = screen.getByRole("button", { name: /Delete Memory/i })
    await waitFor(() =>
      expect(document.activeElement).not.toBe(destructive),
    )
    // The confirm button is also disabled until the word is typed, so
    // even a stray Enter cannot fire it.
    expect((destructive as HTMLButtonElement).disabled).toBe(true)
  })

  it("puts initial focus on the type-to-confirm input", async () => {
    renderModal()
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByPlaceholderText('Type "DELETE" to confirm'),
      ),
    )
  })
})

describe("<TerminateAgentDialog>", () => {
  const renderDialog = () =>
    render(
      <TerminateAgentDialog
        agentId="worker-1"
        open
        onOpenChange={() => {}}
        onConfirmed={() => {}}
      />,
    )

  it("announces as an alertdialog", () => {
    renderDialog()
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("describes the consequence", () => {
    renderDialog()
    expect(describedText(screen.getByRole("alertdialog"))).toMatch(
      /soft-delete/i,
    )
  })

  it("focuses Cancel, the least destructive control", async () => {
    renderDialog()
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "Cancel" }),
      ),
    )
  })
})

describe("<RemoveProjectModal>", () => {
  const renderModal = () =>
    render(
      <RemoveProjectModal projectName="acme" open onOpenChange={() => {}} />,
    )

  it("announces as an alertdialog", () => {
    renderModal()
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("describes the consequence", () => {
    renderModal()
    expect(describedText(screen.getByRole("alertdialog"))).toMatch(
      /Stops the systemd backend/i,
    )
  })

  it("does not focus the destructive button on open", async () => {
    renderModal()
    const remove = screen.getByRole("button", { name: "Remove" })
    await waitFor(() => expect(document.activeElement).not.toBe(remove))
  })
})

// Bug: "Remove alias now" fired the DELETE on a single click with zero
// confirmation — the panel had no dialog at all, unlike every other
// destructive action in the dashboard. Tier 1 is the right gate (the
// action is irreversible but recreatable via Rename, and its blast
// radius is one alias row on one project).
describe("<AliasChipPanel>'s remove-alias confirm", () => {
  const renderPanel = () =>
    render(
      <AliasChipPanel
        projectName="acme"
        alias={{ name: "old-acme", expires_at: "2026-12-31T00:00:00Z" }}
        open
        onClose={() => {}}
      />,
    )

  it("does not call DELETE on a single click — opens a confirm dialog first", async () => {
    routerRequest.mockResolvedValueOnce({ agents: [] })
    const { default: userEvent } = await import("@testing-library/user-event")
    const user = userEvent.setup()
    renderPanel()
    await user.click(
      await screen.findByRole("button", { name: "Remove alias now" }),
    )
    // Only the on-open usage GET has fired — no DELETE yet.
    expect(routerRequest).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("announces as an alertdialog and names the alias", async () => {
    routerRequest.mockResolvedValueOnce({ agents: [] })
    const { default: userEvent } = await import("@testing-library/user-event")
    const user = userEvent.setup()
    renderPanel()
    await user.click(
      await screen.findByRole("button", { name: "Remove alias now" }),
    )
    const dialog = screen.getByRole("alertdialog")
    expect(describedText(dialog)).toMatch(/old-acme/)
  })

  it("does not focus the destructive button on open", async () => {
    routerRequest.mockResolvedValueOnce({ agents: [] })
    const { default: userEvent } = await import("@testing-library/user-event")
    const user = userEvent.setup()
    renderPanel()
    await user.click(
      await screen.findByRole("button", { name: "Remove alias now" }),
    )
    const destructive = screen.getByRole("button", { name: "Remove alias" })
    await waitFor(() => expect(document.activeElement).not.toBe(destructive))
  })
})

// The tier-1 confirm (tasks-leaf, schedules, memories, terminate all
// render this). Same two APG requirements as its type-to-confirm
// sibling — a simple confirm is still a confirmation prompt.
describe("<ConfirmActionModal> (tasks-leaf, schedules, memories, terminate)", () => {
  const renderModal = () =>
    render(
      <ConfirmActionModal
        open
        onOpenChange={() => {}}
        onConfirm={async () => {}}
        title="Delete task"
        description="Delete task \u201cShip the thing\u201d? This cannot be undone."
      />,
    )

  it("announces as an alertdialog", () => {
    renderModal()
    expect(screen.getByRole("alertdialog")).toBeTruthy()
  })

  it("describes the CONSEQUENCE, not just the title", () => {
    renderModal()
    expect(describedText(screen.getByRole("alertdialog"))).toMatch(
      /cannot be undone/i,
    )
  })

  it("focuses Cancel, the least destructive control", async () => {
    renderModal()
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "Cancel" }),
      ),
    )
  })
})
