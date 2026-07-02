/**
 * UX-polish regression guards (fix/ux-polish).
 *
 * Source-text assertions in the house style (pure Node, no jsdom /
 * RTL — see tests/wave-2-no-admin-token.test.ts for the rationale).
 * Each block pins a property of the source bytes that `npm run build`
 * / `tsc` cannot enforce, so a future refactor can't silently unwind
 * the fix without flipping a test.
 *
 *  UX-08  group deletion is gated behind a type-to-confirm input.
 *  UX-09  message-retention validates on blur instead of silently
 *         coercing garbage on Save.
 *  UX-10  create-memory suggestion chips come from the project's real
 *         existing keys (or are hidden when none exist) — never the
 *         old hardcoded fake examples.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// ── UX-08: group delete type-to-confirm guard ─────────────────────

describe("UX-08: group deletion type-to-confirm", () => {
  const src = read("components/dashboard/groups-dashboard.tsx")

  it("compares a typed confirmation against the group name", () => {
    // The guard is `confirmText.trim() === group.name`; if that check
    // disappears the Delete button would no longer require the name.
    expect(
      /confirmText\s*\.trim\(\)\s*===\s*group\.name/.test(src),
      "DeleteGroupModal must compare confirmText to group.name",
    ).toBe(true)
  })

  it("disables the Delete button until the name is confirmed", () => {
    expect(
      /disabled=\{submitting \|\| !confirmed\}/.test(src),
      "Delete button must be disabled while !confirmed",
    ).toBe(true)
  })

  it("guards the delete request behind the confirmation", () => {
    expect(
      /if \(!confirmed\) return/.test(src),
      "handleDelete must bail when not confirmed",
    ).toBe(true)
  })
})

// ── UX-09: retention validates on blur, no silent coercion ────────

describe("UX-09: message-retention validation", () => {
  const src = read("components/dashboard/settings-dashboard.tsx")

  it("defines a validateRetention helper", () => {
    expect(
      /function validateRetention\(/.test(src),
      "settings-dashboard must define validateRetention",
    ).toBe(true)
  })

  it("refuses to save when the draft is invalid (no silent coerce)", () => {
    // saveRetention must consult validateRetention and bail before it
    // ever calls coerceNonNegInt — that's the whole point of UX-09.
    const saveBody = src.match(
      /const saveRetention = async \(\) => \{([\s\S]*?)\n  \}/,
    )
    expect(saveBody, "saveRetention must be declared").not.toBeNull()
    expect(
      /validateRetention\(retention\.draft\) !== null/.test(saveBody![1]),
      "saveRetention must validate before coercing",
    ).toBe(true)
  })

  it("shows an inline hint after blur and disables Save when invalid", () => {
    expect(
      /onBlur=\{\(\) => setRetentionTouched\(true\)\}/.test(src),
      "retention input must mark itself touched on blur",
    ).toBe(true)
    expect(
      /retentionTouched && validateRetention\(retention\.draft\)/.test(src),
      "an inline hint must render when touched and invalid",
    ).toBe(true)
  })
})

// ── UX-10: memory-key chips are real, not fake examples ───────────

describe("UX-10: create-memory suggestion chips", () => {
  const src = read("components/dashboard/modals/create-memory-modal.tsx")

  it("no longer hardcodes fake example keys", () => {
    const FAKE = [
      "api.endpoints.base_url",
      "config.database.connection",
      "settings.ui.theme",
      "memory.system.status",
      "cache.ttl.default",
    ]
    const offenders = FAKE.filter((k) => src.includes(k))
    expect(
      offenders,
      `hardcoded fake suggestion keys still present: ${offenders.join(", ")}`,
    ).toEqual([])
  })

  it("sources chips from the data store's existing context keys", () => {
    expect(
      /useDataStore\(\(s\) => s\.data\?\.context\)/.test(src),
      "chips must read existing keys from the data store",
    ).toBe(true)
    expect(
      /c\?\.context_key/.test(src),
      "existing keys must map from context_key",
    ).toBe(true)
  })

  it("hides the suggestion row entirely when there are no keys", () => {
    expect(
      /existingKeys\.length > 0 &&/.test(src),
      "the chip row must be gated on existingKeys.length > 0",
    ).toBe(true)
  })
})
