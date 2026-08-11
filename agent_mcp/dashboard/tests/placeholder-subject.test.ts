/**
 * Placeholder-subject UI guards (feat/placeholder-subject-ui).
 *
 * Backend Phase 1/2 (PRs #554/#555) store a NULL subject when a message
 * has none, and the read path returns a 50-char body preview + a
 * `subject_is_placeholder` flag. This finishes the UI half: the messages
 * views must render that placeholder CLEVERLY — muted/italic + an "auto"
 * tag — so a human reads it as a stub, not a real subject.
 *
 * Source-text assertions in the house style (pure Node, no jsdom / RTL —
 * see tests/ux-polish.test.ts). Each block pins a property of the source
 * that `tsc` / `npm run build` cannot enforce, so a refactor can't
 * silently drop the placeholder treatment.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { messagesPageSource } from "./support/messages-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) => readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// ── The API type carries the flag ─────────────────────────────────────

describe("Message type exposes subject_is_placeholder", () => {
  it("api.ts Message interface declares the flag", () => {
    // W6-followup F1: the Message type moved from the old lib/api.ts
    // God-module into the per-resource lib/api/messages.ts module.
    const src = read("lib/api/messages.ts")
    expect(src).toMatch(/subject_is_placeholder\??:\s*boolean/)
  })

  it("mobile-list MessageRow declares the flag", () => {
    const src = read("components/dashboard/messages-mobile-list.tsx")
    expect(src).toMatch(/subject_is_placeholder\??:\s*boolean/)
  })
})

// ── Desktop table renders placeholders cleverly ───────────────────────

describe("desktop messages table: placeholder subject", () => {
  // Wave 5 (refactor/w5-messages): the desktop column spec moved into
  // messages/use-messages-columns.tsx. Read the page + satellites as one
  // blob so this guard survives the split.
  const src = messagesPageSource()

  it("gates a distinct branch on subject_is_placeholder", () => {
    expect(src).toMatch(/m\.subject\s*&&\s*m\.subject_is_placeholder/)
  })

  it("renders the placeholder muted + italic with an 'auto' tag", () => {
    // The placeholder branch must be visually distinct: italic + muted +
    // an "auto" marker. Pin all three so none can be dropped silently.
    expect(src).toMatch(/italic/)
    expect(src).toMatch(/text-muted-foreground/)
    expect(src).toMatch(/>\s*auto\s*</i)
  })

  it("still renders a real subject plainly + keeps the reply branch", () => {
    // Regression: the real-subject and reply paths must survive the new
    // leading placeholder branch.
    expect(src).toMatch(/\)\s*:\s*m\.subject\s*\?\s*\(/)
    expect(src).toContain("↳ reply to:")
  })
})

// ── Mobile list renders placeholders cleverly ─────────────────────────

describe("mobile messages list: placeholder subject", () => {
  const src = read("components/dashboard/messages-mobile-list.tsx")

  it("gates a distinct branch on subject_is_placeholder", () => {
    expect(src).toMatch(/m\.subject\s*&&\s*m\.subject_is_placeholder/)
  })

  it("renders the placeholder muted + italic with an 'auto' tag", () => {
    expect(src).toMatch(/italic text-muted-foreground/)
    expect(src).toMatch(/>\s*auto\s*</i)
  })

  it("still renders a real subject + keeps the reply branch", () => {
    expect(src).toMatch(/\)\s*:\s*m\.subject\s*\?\s*\(/)
    expect(src).toContain("↳ reply to:")
  })
})
