// @vitest-environment jsdom
//
// Render pin for the Messages page after its migration onto the shared
// <DataTablePage> scaffold. The page's other guards are source-text
// greps (tests/test_dashboard_messages_*.py, tests/messages-ux.test.ts)
// — they can't tell whether the column spec actually produces the same
// rendered surface the hand-rolled <Table> + <MessagesMobileList> did.
// This mounts the real component (data source stubbed) and asserts the
// surface: header, stats, every column header, one desktop row AND one
// mobile card per message, the reply indent on replies only, both
// pagination footers, and the selection checkboxes.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen, within } from "@testing-library/react"
import { setMatchMedia } from "@/tests/support/match-media"

const message = {
  message_id: "m1",
  sender_id: "backend-dev",
  recipient_id: "manager",
  message_content: "hello world",
  message_type: "text",
  priority: "high",
  timestamp: "2026-01-01T00:00:00Z",
  delivered: 1,
  read: 0,
  subject: "a subject",
  parent_message_id: null,
}
// A reply: no subject, parent set — drives the "↳ reply to" branch and
// the left-border row indent.
const reply = {
  ...message,
  message_id: "m2",
  subject: null,
  parent_message_id: "m1",
  read: 1,
}

// Mutable so individual tests can drive the hook into an error state.
const query = {
  data: [message, reply] as unknown[],
  total: 2,
  loading: false,
  error: null as Error | null,
  refresh: () => {},
  lastFetch: Date.now(),
}

vi.mock("@/hooks/use-paged-query", () => ({
  usePagedQuery: () => query,
}))
vi.mock("@/lib/stores/server-store", () => ({
  useServerStore: () => ({
    servers: [{ id: "s1", name: "proj", status: "connected" }],
    activeServerId: "s1",
  }),
}))
// `ApiError` is needed too: the error path runs through `toastError`,
// which instanceof-checks against it.
vi.mock("@/lib/api", () => ({
  apiClient: { getServerUrl: () => "http://localhost:1/api" },
  ApiError: class ApiError extends Error {},
}))
// The compose recipient dropdown pulls /messages/participants on mount.
vi.stubGlobal(
  "fetch",
  vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({ live: [] }) }),
  ),
)

import { MessagesDashboard } from "@/components/dashboard/messages-dashboard"

// PF-1 (Wave 3): the shared table now renders only the ACTIVE
// breakpoint's tree, chosen via matchMedia (jsdom ships none). Default
// to desktop; the mobile-card test opts into the narrow viewport.
beforeEach(() => setMatchMedia(false))
afterEach(() => {
  cleanup()
  query.data = [message, reply]
  query.total = 2
  query.error = null
})

describe("<MessagesDashboard> (scaffold migration)", () => {
  it("renders the scaffold header + stats strip", () => {
    render(<MessagesDashboard />)
    expect(screen.getByRole("heading", { name: "Messages" })).toBeTruthy()
    expect(
      screen.getByText("Inspect and route inter-agent messages"),
    ).toBeTruthy()
    for (const label of ["Total", "Unread", "Read", "Selected"]) {
      expect(screen.getAllByText(label).length, label).toBeGreaterThan(0)
    }
  })

  it("renders every desktop column header from the column spec", () => {
    const { container } = render(<MessagesDashboard />)
    const table = container.querySelector("table")!
    for (const header of [
      "Time",
      "From",
      "To",
      "Subject",
      "Type",
      "Priority",
      "Read?",
      "Content",
    ]) {
      expect(within(table).getByText(header), header).toBeTruthy()
    }
    expect(screen.getByLabelText("select all visible")).toBeTruthy()
  })

  it("renders a desktop row per message on a desktop viewport", () => {
    setMatchMedia(false)
    const { container } = render(<MessagesDashboard />)
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2)
    // Only the desktop tree exists (PF-1) — no mobile duplicate.
    expect(
      container.querySelector('[data-slot="data-table-mobile"]'),
    ).toBeNull()
    // Per-row checkbox exists once (desktop only).
    expect(screen.getAllByLabelText("select message m1")).toHaveLength(1)
  })

  it("renders a mobile card per message on a mobile viewport", () => {
    setMatchMedia(true)
    const { container } = render(<MessagesDashboard />)
    const mobile = container.querySelector('[data-slot="data-table-mobile"]')!
    expect(mobile.querySelectorAll("li")).toHaveLength(2)
    // Desktop table is absent on the narrow viewport.
    expect(container.querySelector("table")).toBeNull()
    // Per-row checkbox exists once (mobile only).
    expect(screen.getAllByLabelText("select message m1")).toHaveLength(1)
  })

  it("indents reply rows only (rowClassName callback)", () => {
    const { container } = render(<MessagesDashboard />)
    const rows = container.querySelectorAll("tbody tr")
    expect(rows[0]!.className).not.toContain("border-l-2")
    expect(rows[1]!.className).toContain("border-l-2")
  })

  it("renders both pagination footers with the range label", () => {
    render(<MessagesDashboard />)
    expect(screen.getAllByText(/Showing 1–2 of 2/)).toHaveLength(2)
    expect(screen.getAllByLabelText("jump to newest page")).toHaveLength(2)
    expect(screen.getAllByLabelText("jump to oldest page")).toHaveLength(2)
  })

  // Messages is now wired straight into the scaffold's `error` prop —
  // safe only because the scaffold keeps content when rows are in hand.
  it("keeps the message rows when a background refresh fails", () => {
    query.error = new Error("network down")
    const { container } = render(<MessagesDashboard />)
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2)
    expect(screen.queryByText(/Connection Error/i)).toBeNull()
    expect(
      container.querySelector('[data-slot="stale-notice"]'),
    ).toBeTruthy()
  })

  it("shows the full error panel when the first load fails empty", () => {
    query.error = new Error("network down")
    query.data = []
    query.total = 0
    render(<MessagesDashboard />)
    expect(screen.getByText(/Connection Error/i)).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Messages" })).toBeNull()
  })

  it("keeps the filter bar controls", () => {
    render(<MessagesDashboard />)
    expect(screen.getByLabelText("Search messages")).toBeTruthy()
    expect(screen.getByLabelText("Filter by type")).toBeTruthy()
    expect(screen.getByLabelText("Filter by priority")).toBeTruthy()
    expect(screen.getByLabelText("Filter by read status")).toBeTruthy()
  })
})
