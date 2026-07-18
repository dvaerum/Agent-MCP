/**
 * Messages page UX pass — source-text regression guards.
 *
 * Source-text assertions in the house style (pure Node reading .tsx /
 * .ts source, no jsdom / RTL — see tests/ux-polish.test.ts for the
 * rationale). Each block pins a property of the source bytes that
 * `npm run build` / `tsc` cannot enforce, so a future refactor can't
 * silently unwind a fix without flipping a test.
 *
 *  MUX-1  thread view scrolls the opened message into view (ref +
 *         scrollIntoView on the opened ConversationRow).
 *  MUX-2  desktop row shows a real unread signal (bold + leading dot).
 *  MUX-3  priority + type render as colored badges via a shared helper.
 *  MUX-4  truncated subject / content carry a title tooltip.
 *  MUX-5  the list background-refreshes on an interval.
 *  MUX-6  empty-state branches on whether a filter is active.
 *  MUX-7  a11y: compose htmlFor/id, filter aria-labels, sr-only read
 *         state, modal toggle aria-pressed.
 *  MUX-8  reply recipient options always include the selected value.
 *  MUX-9  search placeholder broadened to "Search messages…".
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

const dash = read("components/dashboard/messages-dashboard.tsx")
const mobile = read("components/dashboard/messages-mobile-list.tsx")
const modal = read("components/dashboard/modals/view-message-modal.tsx")
const badges = read("components/dashboard/shared/message-badges.ts")

// ── MUX-1: thread scrolls to the opened message ───────────────────

describe("MUX-1: thread view scrolls to the opened message", () => {
  it("attaches a ref to the opened conversation row", () => {
    // The opened row carries a ref so the modal can target it.
    expect(
      /innerRef=\{opened \? openedRowRef : undefined\}/.test(modal),
      "opened ConversationRow must receive openedRowRef",
    ).toBe(true)
    expect(
      /ref=\{innerRef\}/.test(modal),
      "ConversationRow must forward innerRef onto its element",
    ).toBe(true)
  })

  it("calls scrollIntoView on the opened row after load", () => {
    expect(
      /scrollIntoView\(\{\s*block:\s*["']center["']\s*\}\)/.test(modal),
      "modal must scrollIntoView({ block: 'center' }) the opened row",
    ).toBe(true)
  })

  it("no-ops for single-message threads", () => {
    // Guards against scrolling when there's no conversation container.
    expect(
      /thread\.length <= 1/.test(modal),
      "scroll effect must bail for single-message threads",
    ).toBe(true)
  })
})

// ── MUX-2: desktop unread signal ──────────────────────────────────

describe("MUX-2: desktop row unread signal", () => {
  it("renders a leading unread dot on unread rows", () => {
    expect(
      /!isRead &&[\s\S]{0,120}rounded-full bg-primary/.test(dash),
      "unread desktop rows must show a bg-primary dot",
    ).toBe(true)
  })

  it("bolds the unread sender / subject", () => {
    expect(
      /!isRead && "font-semibold"/.test(dash),
      "unread sender badge must be font-semibold",
    ).toBe(true)
    expect(
      /!isRead && "font-semibold text-foreground"/.test(dash),
      "unread subject cell must be bold",
    ).toBe(true)
  })
})

// ── MUX-3: priority + type badges ─────────────────────────────────

describe("MUX-3: priority + type colored badges", () => {
  it("defines a shared badge-color helper", () => {
    expect(/export const priorityBadgeClass/.test(badges)).toBe(true)
    expect(/export const messageTypeBadgeClass/.test(badges)).toBe(true)
  })

  it("makes urgent + high priority stand out", () => {
    // urgent -> destructive tint, high -> orange/warning tint.
    expect(/urgent:[\s\S]{0,80}destructive/.test(badges)).toBe(true)
    expect(/high:[\s\S]{0,80}orange/.test(badges)).toBe(true)
  })

  it("desktop, mobile, and modal all use the helper", () => {
    for (const src of [dash, mobile, modal]) {
      expect(/priorityBadgeClass\(/.test(src)).toBe(true)
      expect(/messageTypeBadgeClass\(/.test(src)).toBe(true)
    }
  })
})

// ── MUX-4: tooltips on truncated text ─────────────────────────────

describe("MUX-4: title tooltips on truncated text", () => {
  it("desktop subject + content carry a title", () => {
    expect(
      /<span title=\{m\.subject\}>\{m\.subject\}<\/span>/.test(dash),
      "real subject must have a title tooltip",
    ).toBe(true)
    expect(
      /title=\{m\.message_content\}/.test(dash),
      "content cell must have a title tooltip",
    ).toBe(true)
  })

  it("mobile content carries a title", () => {
    expect(/title=\{m\.message_content\}/.test(mobile)).toBe(true)
  })
})

// ── MUX-5: auto-refresh interval ──────────────────────────────────

describe("MUX-5: background refresh interval", () => {
  it("defines a REFRESH_INTERVAL and wires a setInterval", () => {
    expect(/const REFRESH_INTERVAL = /.test(dash)).toBe(true)
    expect(
      /setInterval\([\s\S]{0,60}REFRESH_INTERVAL\)/.test(dash),
      "must schedule a refresh on REFRESH_INTERVAL",
    ).toBe(true)
  })

  it("refreshes in place (does not reset the cursor)", () => {
    // The background tick calls refreshQuery (in-place) — never
    // setCurrentOffset — so the user's page/scroll is preserved.
    const effect = dash.match(
      /const interval = setInterval\(\(\) => \{([\s\S]*?)\}, REFRESH_INTERVAL\)/,
    )
    expect(effect, "background-refresh interval must exist").not.toBeNull()
    expect(/refreshQuery\(\)/.test(effect![1])).toBe(true)
    expect(/setCurrentOffset/.test(effect![1])).toBe(false)
  })

  it("pauses while compose is open", () => {
    expect(
      /if \(composeOpen\) return/.test(dash),
      "background refresh must pause while composing",
    ).toBe(true)
  })
})

// ── MUX-6: empty-state copy branch ────────────────────────────────

describe("MUX-6: empty-state branches on active filters", () => {
  it("derives hasActiveFilters", () => {
    expect(/const hasActiveFilters = /.test(dash)).toBe(true)
  })

  it("shows a plain 'no messages yet' when no filter is set", () => {
    expect(/No messages yet/.test(dash)).toBe(true)
    // The filtered branch keeps the Clear-filters CTA.
    expect(/No messages match the current filters\./.test(dash)).toBe(true)
    expect(/hasActiveFilters \? \(/.test(dash)).toBe(true)
  })
})

// ── MUX-7: accessibility batch ────────────────────────────────────

describe("MUX-7: accessibility", () => {
  it("compose inputs are label-associated via htmlFor/id", () => {
    for (const id of [
      "compose-recipient",
      "compose-type",
      "compose-priority",
      "compose-subject",
      "compose-content",
    ]) {
      expect(
        new RegExp(`htmlFor="${id}"`).test(dash),
        `Label htmlFor="${id}" must exist`,
      ).toBe(true)
      expect(
        new RegExp(`id="${id}"`).test(dash),
        `input id="${id}" must exist`,
      ).toBe(true)
    }
  })

  it("filter controls carry aria-labels", () => {
    expect(/aria-label="Search messages"/.test(dash)).toBe(true)
    expect(/aria-label="Filter by type"/.test(dash)).toBe(true)
    expect(/aria-label="Filter by priority"/.test(dash)).toBe(true)
    expect(/aria-label="Filter by read status"/.test(dash)).toBe(true)
    expect(/ariaLabel="Filter by sender"/.test(dash)).toBe(true)
    expect(/ariaLabel="Filter by recipient"/.test(dash)).toBe(true)
  })

  it("Read? column has sr-only read/unread text", () => {
    expect(
      /<span className="sr-only">\{isRead \? "read" : "unread"\}<\/span>/.test(
        dash,
      ),
      "Read? column must name the state for screen readers",
    ).toBe(true)
  })

  it("modal read toggles announce state via aria-pressed", () => {
    expect(
      /aria-pressed=\{isRead\}/.test(modal),
      "modal read toggle must set aria-pressed={isRead}",
    ).toBe(true)
  })
})

// ── MUX-8: offline-recipient reply ────────────────────────────────

describe("MUX-8: recipient options include the selected value", () => {
  it("appends composeRecipient when not a live participant", () => {
    expect(
      /ids\.add\(composeRecipient\)/.test(dash),
      "recipientOptions must include the currently-selected recipient",
    ).toBe(true)
    // memoised on composeRecipient so it re-includes on reply.
    expect(
      /\}, \[liveParticipants, composeRecipient\]\)/.test(dash),
      "recipientOptions must depend on composeRecipient",
    ).toBe(true)
  })
})

// ── MUX-9: broadened search placeholder ───────────────────────────

describe("MUX-9: search placeholder broadened", () => {
  it("placeholder reads 'Search messages…' not 'Search content'", () => {
    expect(/placeholder="Search messages\.\.\."/.test(dash)).toBe(true)
    expect(/Search content/.test(dash)).toBe(false)
  })
})
