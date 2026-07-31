// @vitest-environment jsdom
//
// Mobile-overflow regression for the message-detail modal. A long
// recipient_id (e.g. "pikvm-mcp-server@georgs-mac-mini") in the footer's
// "Reply as {recipient_id}" button carries shadcn's `whitespace-nowrap`,
// so on a narrow viewport its non-wrapping min-content forces the
// DialogContent GRID track wider than the dialog (grid children default
// to `min-width: auto`) — the whole popup then overflows and clips on the
// right (reported on iOS Safari over the tailnet). jsdom can't measure
// layout, so this pins the STRUCTURAL guards that stop the blowout:
//   1. DialogContent lets its grid children shrink ([&>*]:min-w-0) and
//      hides residual overflow.
//   2. The reply label truncates (min-w-0 + a `truncate` span) instead of
//      forcing width.
// RED before the fix, GREEN after.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, waitFor } from "@testing-library/react"

// The modal fetches its thread on open; return just the opened message so
// it renders the single-message detail (footer + Reply button) without
// network.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getMessageThread: vi.fn(() => Promise.resolve([longRecipientMessage])),
  }
})

import { ViewMessageModal } from "@/components/dashboard/modals/view-message-modal"
import type { Message } from "@/lib/api"

const longRecipientMessage: Message = {
  message_id: "m-overflow",
  sender_id: "manager",
  // The load-bearing input: a long, unbreakable recipient token.
  recipient_id: "pikvm-mcp-server@georgs-mac-mini",
  message_content: "body",
  message_type: "chat",
  priority: "normal",
  timestamp: "2026-01-01T00:00:00Z",
  delivered: 1,
  read: 0,
  subject: "s",
  parent_message_id: null,
}

afterEach(() => cleanup())

describe("ViewMessageModal does not overflow on a long recipient_id", () => {
  it("DialogContent lets grid children shrink and clips residual overflow", async () => {
    render(
      <ViewMessageModal
        message={longRecipientMessage}
        open
        onOpenChange={() => {}}
        onReply={() => {}}
        onToggleRead={() => {}}
        onDelete={() => {}}
      />,
    )
    const content = await waitFor(() => {
      const el = document.querySelector('[data-slot="dialog-content"]')
      if (!el) throw new Error("dialog not open")
      return el as HTMLElement
    })
    const cls = content.className
    // Grid children must be allowed to shrink below their min-content,
    // otherwise the nowrap reply button forces the whole grid wider.
    expect(cls).toContain("[&>*]:min-w-0")
    expect(cls).toContain("overflow-hidden")
  })

  it("the reply button truncates its label instead of forcing width", async () => {
    render(
      <ViewMessageModal
        message={longRecipientMessage}
        open
        onOpenChange={() => {}}
        onReply={() => {}}
        onToggleRead={() => {}}
        onDelete={() => {}}
      />,
    )
    const replyBtn = await waitFor(() => {
      const btn = [...document.querySelectorAll("button")].find((b) =>
        (b.textContent || "").includes("Reply as"),
      )
      if (!btn) throw new Error("reply button not found")
      return btn
    })
    // The full recipient must be present (not silently dropped)…
    expect(replyBtn.textContent).toContain("pikvm-mcp-server@georgs-mac-mini")
    // …but the button must be shrinkable and the label must truncate.
    expect(replyBtn.className).toContain("min-w-0")
    expect(replyBtn.querySelector(".truncate")).not.toBeNull()
  })
})
