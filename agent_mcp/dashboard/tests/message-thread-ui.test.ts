/**
 * Feature 1 (message-threads-ui) — conversation-view guards.
 *
 * Source-text assertions in the house style (pure Node reading .tsx /
 * .ts source, no jsdom / RTL — see tests/ux-polish.test.ts for the
 * rationale). Each block pins a property of the source bytes that
 * `npm run build` / `tsc` cannot enforce.
 *
 *  MT-1  api.ts declares getMessageThread hitting the /thread endpoint.
 *  MT-2  view-message-modal fetches the thread on open and renders a
 *        flat chronological conversation (maps over multiple messages).
 *  MT-3  the modal highlights the message the user actually clicked.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// ── MT-1: api.ts getMessageThread ─────────────────────────────────

describe("MT-1: api.ts declares getMessageThread", () => {
  const src = read("lib/api.ts")

  it("exports a getMessageThread function", () => {
    expect(
      /export async function getMessageThread\s*\(/.test(src),
      "api.ts must export async getMessageThread",
    ).toBe(true)
  })

  it("hits the /thread endpoint and returns data.thread", () => {
    expect(
      /\/thread/.test(src),
      "getMessageThread must hit the /{message_id}/thread endpoint",
    ).toBe(true)
    expect(
      /\.thread\b/.test(src),
      "getMessageThread must return data.thread",
    ).toBe(true)
  })
})

// ── MT-2 / MT-3: conversation view in the modal ───────────────────

describe("MT-2: view-message-modal renders a conversation", () => {
  const src = read("components/dashboard/modals/view-message-modal.tsx")

  it("imports and calls getMessageThread", () => {
    expect(
      /getMessageThread/.test(src),
      "modal must call getMessageThread",
    ).toBe(true)
  })

  it("fetches the thread for the opened message id", () => {
    // The fetch is keyed on the clicked message's id.
    expect(
      /getMessageThread\([^)]*message\.message_id/.test(src),
      "modal must fetch the thread for message.message_id",
    ).toBe(true)
  })

  it("maps over the fetched thread to render each message", () => {
    // A flat chronological conversation = iterating the thread array.
    expect(
      /thread\s*\.map\(/.test(src) || /\.map\(\s*\(?\s*msg/.test(src),
      "modal must map over the thread messages",
    ).toBe(true)
  })

  it("stores the fetched thread in state", () => {
    expect(
      /useState<\s*Message\[\]/.test(src),
      "modal must hold the thread as Message[] state",
    ).toBe(true)
  })
})

describe("MT-3: modal highlights the clicked message", () => {
  const src = read("components/dashboard/modals/view-message-modal.tsx")

  it("compares each row's id against the opened message_id", () => {
    // The ring/accent is gated on the row being the one the user opened.
    expect(
      /message_id\s*===\s*message\.message_id/.test(src) ||
        /message\.message_id\s*===/.test(src),
      "modal must compare a row id against message.message_id to highlight it",
    ).toBe(true)
  })

  it("applies a ring/accent highlight class", () => {
    expect(
      /ring/.test(src),
      "modal must apply a ring highlight to the opened message",
    ).toBe(true)
  })
})

// ── MT-4: per-message read status + toggle (envelope: open=read, closed=unread)

describe("MT-4: per-row read status + toggle", () => {
  const modal = read("components/dashboard/modals/view-message-modal.tsx")
  const dash = read("components/dashboard/messages-dashboard.tsx")

  it("ConversationRow takes a per-message onToggleRead", () => {
    expect(/onToggleRead:\s*\(msg:\s*Message\)\s*=>\s*void/.test(modal)).toBe(true)
  })

  it("each row shows the envelope read state and toggles that message", () => {
    // closed envelope (Mail) = unread, open (MailOpen) = read; clicking
    // calls onToggleRead(msg) so it's unambiguous which message flips.
    expect(/onToggleRead\(msg\)/.test(modal)).toBe(true)
    expect(/Mail\b/.test(modal) && /MailOpen\b/.test(modal)).toBe(true)
  })

  it("no ✓/✗ glyphs remain — icons replace them", () => {
    expect(modal.includes("✓") || modal.includes("✗")).toBe(false)
  })

  it("the ambiguous no-arg footer Mark-read wiring is gone", () => {
    // the old single footer button did onClick={onToggleRead} (no arg,
    // silently the opened message); the per-row toggle replaces it.
    expect(/onClick=\{onToggleRead\}/.test(modal)).toBe(false)
  })

  it("parent passes the specific message to onToggleRead", () => {
    expect(/onToggleRead=\{\(msg\)\s*=>\s*\{?\s*void toggleRead\(msg\)/.test(dash)).toBe(true)
  })
})
