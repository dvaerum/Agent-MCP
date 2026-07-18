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
import {
  priorityBadgeClass,
  messageTypeBadgeClass,
} from "../components/dashboard/shared/message-badges"

// Keep in sync with the option lists in messages-dashboard.tsx.
const ALL_TYPES = [
  "text",
  "system",
  "notification",
  "task_update",
  "assistance_request",
]
const ALL_PRIORITIES = ["low", "normal", "high", "urgent"]

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
  it("placeholder names the broadened search scope, not just content", () => {
    // The field now has a visible "Search" label above it, so the
    // placeholder describes WHAT is searched (subject/sender/recipient/
    // content) — reflecting the #559 broadened backend search.
    expect(/placeholder="subject, sender, recipient, content/.test(dash)).toBe(true)
    expect(/placeholder="Search content/.test(dash)).toBe(false)
  })
})

// ── MUX-10: robust to long / varied messages ──────────────────────

describe("MUX-10: long-content robustness", () => {
  it("conversation rows wrap long bodies (break-words)", () => {
    // Both <pre> blocks (conversation row + single-message detail) must
    // wrap so a 2000-char body or a long unbroken URL/base64 token can't
    // blow out the modal width. A refactor dropping break-words would
    // reintroduce horizontal overflow — this guard fails if it does.
    const preBlocks = modal.match(/<pre[^>]*className="[^"]*"/g) ?? []
    expect(preBlocks.length).toBeGreaterThanOrEqual(2)
    for (const pre of preBlocks) {
      expect(/break-words/.test(pre), `<pre> must break-words: ${pre}`).toBe(
        true,
      )
    }
    // The conversation row also honors newlines (whitespace-pre-wrap).
    expect(/whitespace-pre-wrap break-words/.test(modal)).toBe(true)
  })

  it("conversation scroll container is height-capped", () => {
    // Tall (wrapped) rows must stay inside the scroll container so the
    // scroll-to-opened (MUX-1) lands correctly rather than growing the
    // modal unbounded.
    expect(/max-h-\[55vh\] overflow-auto/.test(modal)).toBe(true)
  })

  it("mobile content wraps long tokens (break-words)", () => {
    expect(/line-clamp-2 break-words/.test(mobile)).toBe(true)
  })

  it("desktop content + subject cells clip with a tooltip (no overflow)", () => {
    // Table cells stay single-line via truncate + a title tooltip so a
    // huge body clips instead of forcing table-wide horizontal overflow.
    expect(/max-w-\[400px\] truncate/.test(dash)).toBe(true)
    expect(/max-w-\[200px\] truncate/.test(dash)).toBe(true)
  })

  it("long sender/recipient ids truncate instead of growing the column", () => {
    // Desktop From/To cells are width-capped and the badge truncates.
    expect(
      (dash.match(/<TableCell className="max-w-\[160px\]">/g) ?? []).length,
    ).toBeGreaterThanOrEqual(2)
    // Mobile caps the id badges too.
    expect(
      (mobile.match(/max-w-\[40%\]/g) ?? []).length,
    ).toBeGreaterThanOrEqual(2)
  })
})

// ── MUX-11: badge helper covers every type + priority ─────────────

describe("MUX-11: badge helper — every type + priority", () => {
  it("returns a non-empty class for every message_type", () => {
    for (const t of ALL_TYPES) {
      expect(messageTypeBadgeClass(t).trim().length, `type ${t}`).toBeGreaterThan(0)
    }
    // Unknown types fall back, never crash / return empty.
    expect(messageTypeBadgeClass("something_new").trim().length).toBeGreaterThan(0)
  })

  it("returns a non-empty class for every priority", () => {
    for (const p of ALL_PRIORITIES) {
      expect(priorityBadgeClass(p).trim().length, `priority ${p}`).toBeGreaterThan(0)
    }
    expect(priorityBadgeClass("whatever").trim().length).toBeGreaterThan(0)
  })

  it("urgent + high stand out from normal + low", () => {
    const urgent = priorityBadgeClass("urgent")
    const high = priorityBadgeClass("high")
    const normal = priorityBadgeClass("normal")
    const low = priorityBadgeClass("low")
    // urgent = destructive tint, high = orange tint — both distinct from
    // the muted normal/low treatment.
    expect(urgent).toContain("destructive")
    expect(high).toContain("orange")
    expect(urgent).not.toEqual(normal)
    expect(high).not.toEqual(normal)
    expect(urgent).not.toEqual(low)
  })

  it("badges never wrap (whitespace-nowrap comes from the Badge cva)", () => {
    // The longest labels (assistance_request / notification) must not
    // wrap oddly — the shared Badge keeps whitespace-nowrap, and the
    // helper only adds colour utilities, never a wrap/width override.
    for (const t of ALL_TYPES) {
      expect(/wrap|w-\[/.test(messageTypeBadgeClass(t))).toBe(false)
    }
  })
})

// ── MUX-12: filter controls carry a visible label ──────────────────
describe("MUX-12: filter dropdowns have visible labels", () => {
  const src = read("components/dashboard/messages-dashboard.tsx")
  it("wraps each filter in a labeled FilterField (From/To/Type/Priority/Status)", () => {
    expect(/const FilterField/.test(src)).toBe(true)
    for (const label of ["Search", "From", "To", "Type", "Priority", "Status"]) {
      expect(
        new RegExp(`<FilterField label="${label}"`).test(src),
        `missing visible label "${label}"`,
      ).toBe(true)
    }
  })
})
