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
import { groupsPageSource } from "@/tests/support/groups-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// ── UX-08: group delete type-to-confirm guard ─────────────────────

// The guard used to live in a bespoke `DeleteGroupModal` inside
// groups-dashboard.tsx (`confirmText.trim() === group.name`). The
// shared-scaffold migration retired that copy in favour of the unified
// <DeleteConfirmModal> with `requiredWord` + `matchCase`. UX-08 is
// therefore pinned in two halves: the page must still demand the
// group's exact NAME, and the shared modal must still gate on it.
//
// Wave 5 extraction: the Groups page is now a page + a `groups/`
// satellite folder, so this reads the whole page blob (page + all
// satellites) rather than the single orchestrator file — the delete
// gate stays in the orchestrator today, but reading the blob keeps the
// assertion honest if it ever moves into a satellite.
describe("UX-08: group deletion type-to-confirm", () => {
  const src = groupsPageSource()
  const modal = read("components/dashboard/modals/delete-confirm-modal.tsx")

  it("requires the group's own name as the confirmation word", () => {
    expect(
      /requiredWord=\{deleteTarget\?\.name/.test(src),
      "groups-dashboard must pass the group name as requiredWord",
    ).toBe(true)
  })

  it("confirms the name case-sensitively (no 'devs' for 'Devs')", () => {
    expect(
      /matchCase/.test(src),
      "groups-dashboard must pass matchCase so the name is exact",
    ).toBe(true)
    expect(
      /matchCase\s*\n?\s*\?\s*confirmationText === requiredWord/.test(modal),
      "DeleteConfirmModal must compare exactly when matchCase is set",
    ).toBe(true)
  })

  it("disables the Delete button until the name is confirmed", () => {
    expect(
      /disabled=\{loading \|\| !isConfirmed\}/.test(modal),
      "Delete button must be disabled while !isConfirmed",
    ).toBe(true)
  })

  it("guards the delete request behind the confirmation", () => {
    expect(
      /if \(!isConfirmed\) return/.test(modal),
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
    // ADR-0018: the generic saveField must consult validateRetention
    // for the int_days widget and bail before it ever coerces — that's
    // the whole point of UX-09.
    const saveBody = src.match(
      /const saveField = async \(entry: SettingsSchemaEntry\) => \{([\s\S]*?)\n  \}/,
    )
    expect(saveBody, "saveField must be declared").not.toBeNull()
    expect(
      /kind === "int_days" && validateRetention\(draft\) !== null/.test(
        saveBody![1]!,
      ),
      "saveField must validate the int_days draft before coercing",
    ).toBe(true)
  })

  it("shows an inline hint after blur and disables Save when invalid", () => {
    // The int_days control marks itself touched on blur and only then
    // renders the validation hint.
    expect(
      /onBlur=\{onBlur\}/.test(src),
      "int_days input must mark itself touched on blur",
    ).toBe(true)
    expect(
      /setTouched\(\(t\) => \(\{ \.\.\.t, \[entry\.key\]: true \}\)\)/.test(src),
      "the blur handler must flip the entry's touched flag",
    ).toBe(true)
    expect(
      /touched && invalid && \(/.test(src),
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

  it("sources chips from the shared all-data query's existing context keys", () => {
    // Wave 6: context rows come from the `/all-data` TanStack Query via
    // `useContextRows()` (was `useDataStore((s) => s.data?.context)`).
    expect(
      /useContextRows\(\)/.test(src),
      "chips must read existing keys from the all-data query",
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
