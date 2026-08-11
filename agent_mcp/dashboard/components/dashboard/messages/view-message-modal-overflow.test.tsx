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
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { render, cleanup, waitFor } from "@testing-library/react"

// The modal fetches its thread on open. Default: just the opened message
// (single-message detail — footer + Reply button). The conversation test
// overrides this with a multi-message thread so ConversationRow renders.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getMessageThread: vi.fn(),
  }
})

import { ViewMessageModal } from "@/components/dashboard/modals/view-message-modal"
import { getMessageThread, type Message } from "@/lib/api"

const mockThread = vi.mocked(getMessageThread)

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

beforeEach(() => {
  // jsdom has no layout engine; the modal scrolls the opened row into view
  // once a conversation loads. Stub it so the effect doesn't throw.
  Element.prototype.scrollIntoView = vi.fn()
  // Default single-message thread; the conversation test overrides.
  mockThread.mockResolvedValue([longRecipientMessage])
})
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
    // Children must be allowed to shrink below their min-content,
    // otherwise the nowrap reply button forces the whole dialog wider.
    expect(cls).toContain("[&>*]:min-w-0")
    expect(cls).toContain("overflow-hidden")
    // …and the dialog must be height-capped to the viewport with a
    // flex column so a tall conversation scrolls internally instead of
    // overflowing above/below the screen.
    expect(cls).toContain("flex-col")
    expect(cls).toContain("max-h-[calc(100dvh-2rem)]")
  })

  it("the message area flexes and scrolls instead of growing the dialog", async () => {
    mockThread.mockResolvedValue([longRecipientMessage])
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
    // Single-message detail: the content <pre> is the scroll region — it
    // must flex (flex-1 min-h-0) + overflow-auto, not a fixed max-height
    // that (with header+footer) can still exceed the viewport.
    const pre = await waitFor(() => {
      const el = document.querySelector('[data-slot="dialog-content"] pre')
      if (!el) throw new Error("content pre not found")
      return el as HTMLElement
    })
    expect(pre.className).toContain("flex-1")
    expect(pre.className).toContain("min-h-0")
    expect(pre.className).toContain("overflow-auto")
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

describe("ViewMessageModal conversation-row from→to header", () => {
  const second: Message = {
    ...longRecipientMessage,
    message_id: "m-2",
    sender_id: "pikvm-mcp-server@georgs-mac-mini",
    recipient_id: "manager",
    parent_message_id: "m-overflow",
  }

  it("stacks the sender→recipient line on mobile so ids don't char-stack", async () => {
    mockThread.mockResolvedValue([longRecipientMessage, second])
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
    // The sender id text node must be intact (not split char-by-char) and
    // live inside a min-w-0 group within a header that stacks on mobile
    // (flex-col) and only goes side-by-side from sm up (sm:flex-row).
    const senderSpan = await waitFor(() => {
      const el = [...document.querySelectorAll("span.font-medium")].find(
        (s) => s.textContent === "manager",
      )
      if (!el) throw new Error("sender id span not found")
      return el as HTMLElement
    })
    const idGroup = senderSpan.parentElement as HTMLElement
    expect(idGroup.className).toContain("min-w-0")
    const header = idGroup.parentElement as HTMLElement
    expect(header.className).toContain("flex-col")
    expect(header.className).toContain("sm:flex-row")
  })
})
