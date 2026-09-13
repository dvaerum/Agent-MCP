/**
 * Regression guards for the dashboard Messages tab (Phase 6 PR #21).
 *
 * The tab adds a new top-level dashboard view that lets admins:
 * - See message history with filters.
 * - Compose new messages to agents.
 * - Mark messages read/unread.
 *
 * Tests verify the component file exists, the view is wired into the
 * top-level switch, and the navigation sidebar lists it. Regression
 * guards (parse .ts(x) text) since we don't have jsdom infrastructure;
 * behavior verified by `npm run build` + manual click-through.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { messagesPageSource } from "./support/messages-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// Wave 5 (refactor/w5-messages): read the Messages page + its
// `messages/` satellites as one blob so these page-level guards
// survive the god-file split. `messages-mobile-list.tsx` is read
// directly (it's part of the blob but a couple of guards target it by
// name). See tests/support/messages-source.ts.
const readMessagesDashboard = () => messagesPageSource()

describe("Messages dashboard component", () => {
  it("exists and calls the /api/messages endpoint", () => {
    const src = readMessagesDashboard()
    // Must export the component used by page.tsx.
    expect(
      src.includes("export function MessagesDashboard") ||
        src.includes("export const MessagesDashboard"),
    ).toBe(true)
    // Must reference the REST endpoint (issue P, PR #20).
    expect(
      src.includes("/api/messages"),
      "expected the component to call /api/messages (the new REST endpoint)",
    ).toBe(true)
  })
})

describe("Messages view wiring", () => {
  it("page.tsx routes the messages view to the component", () => {
    const src = read("app/page.tsx")
    expect(
      src.includes("MessagesDashboard"),
      "expected page.tsx to import + render MessagesDashboard",
    ).toBe(true)
    expect(
      src.includes("'messages'") || src.includes('"messages"'),
      "expected page.tsx switch to include a 'messages' case",
    ).toBe(true)
  })

  it("store.ts currentView union includes messages", () => {
    const src = read("lib/store.ts")
    // Find the currentView union and check 'messages' is in it.
    expect(
      src.includes("'messages'") || src.includes('"messages"'),
      "expected store.ts currentView union to include 'messages'",
    ).toBe(true)
  })

  it("navigation lists the messages tab", () => {
    const src = read("components/layout/navigation.tsx")
    expect(
      src.includes("'messages'") || src.includes('"messages"'),
      "expected navigation.tsx NavItem entry for view: 'messages'",
    ).toBe(true)
    // Common icon for messages is MessageSquare or Mail.
    expect(
      ["MessageSquare", "Mail", "MessagesSquare"].some((icon) => src.includes(icon)),
      "expected a message-icon import (MessageSquare/Mail/MessagesSquare)",
    ).toBe(true)
  })
})

// ---------- v5.0.22: subject column + reply indent + Suggest button --------

describe("v5.0.22 subject column + reply indent + Suggest button", () => {
  it("renders a Subject column", () => {
    // The table must render a Subject column so threading is visible.
    const src = readMessagesDashboard()
    expect(
      src.includes(">Subject<") || src.includes('"Subject"') || src.includes("'Subject'"),
      "expected a Subject column header in messages-dashboard.tsx",
    ).toBe(true)
    // The rendered subject must come from `row.subject` (the new
    // column) — guards against a hardcoded label.
    expect(
      src.includes(".subject"),
      "expected the row's `.subject` field to be rendered in the table",
    ).toBe(true)
  })

  it("renders a reply indent for threaded rows", () => {
    // Rows whose `parent_message_id` is non-null must render the
    // reply marker so the dashboard surfaces email-style threading.
    const src = readMessagesDashboard()
    expect(
      src.includes("parent_message_id"),
      "expected messages-dashboard.tsx to branch on parent_message_id",
    ).toBe(true)
    const hasReplyLabel = src.includes("reply to") || src.includes("↳")
    const hasIndentClass =
      (src.includes("border-l") && src.includes("parent_message_id")) ||
      src.includes("reply-indent")
    expect(
      hasReplyLabel || hasIndentClass,
      "expected either a '↳ reply to' label or a left-border indent " +
        "class toggled by parent_message_id",
    ).toBe(true)
  })

  it("compose form has a Subject input + Suggest button", () => {
    // Compose form gains a Subject input + a Suggest button.
    const src = readMessagesDashboard()
    expect(
      src.includes("Subject"),
      "expected a Subject label/placeholder in the compose form",
    ).toBe(true)
    expect(
      src.includes("Suggest"),
      "expected a 'Suggest' button in the compose form",
    ).toBe(true)
    expect(
      src.includes("/api/messages/suggest-subject"),
      "expected the Suggest button to call /api/messages/suggest-subject",
    ).toBe(true)
  })

  it("mobile list renders subject and reply marker", () => {
    // The mobile list mirrors desktop: subject visible, replies marked.
    const src = read("components/dashboard/messages-mobile-list.tsx")
    expect(
      src.includes(".subject"),
      "expected messages-mobile-list.tsx to render the row's subject",
    ).toBe(true)
    expect(
      src.includes("parent_message_id"),
      "expected messages-mobile-list.tsx to branch on parent_message_id",
    ).toBe(true)
  })
})
