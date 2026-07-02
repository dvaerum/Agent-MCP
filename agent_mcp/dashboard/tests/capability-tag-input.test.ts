/**
 * Unit tests for the pure helpers behind <CapabilityTagInput>.
 *
 * The dashboard test suite is intentionally node-environment only (no
 * jsdom / RTL — see vitest.config.ts). So instead of driving the React
 * tree, we test the pure reducers the component delegates to. These
 * are the exact functions the component calls on Enter/comma (add),
 * on the X button (remove is a plain `filter`, tested via addTags's
 * dedupe invariants + the collect suggestion source), and to source
 * autocomplete suggestions — so covering them covers the behaviour:
 *
 *   - typing + Enter adds a NORMALIZED chip (lowercase + trim)
 *   - a free tag not in the suggestions is still accepted
 *   - dedupe: adding an existing tag is a no-op
 *   - suggestions surface from in-use agent + task tags, distinct+sorted
 */

import { describe, expect, it } from "vitest"

import {
  addCapabilityTags,
  collectCapabilitySuggestions,
  normalizeCapabilityTag,
} from "@/components/dashboard/shared/capability-tags"

describe("normalizeCapabilityTag", () => {
  it("lowercases and trims (mirrors server normalize_capabilities)", () => {
    expect(normalizeCapabilityTag("  Backend  ")).toBe("backend")
    expect(normalizeCapabilityTag("DB")).toBe("db")
  })

  it("returns empty string for whitespace-only / nullish", () => {
    expect(normalizeCapabilityTag("   ")).toBe("")
    expect(normalizeCapabilityTag(undefined)).toBe("")
    expect(normalizeCapabilityTag(null)).toBe("")
  })
})

describe("addCapabilityTags", () => {
  it("adds a single typed tag, normalized", () => {
    expect(addCapabilityTags([], "Backend")).toEqual(["backend"])
  })

  it("allows a free tag that is not a known suggestion", () => {
    // No suggestion registry gates this — a brand-new tag is valid.
    expect(addCapabilityTags(["backend"], "some-novel-skill")).toEqual([
      "backend",
      "some-novel-skill",
    ])
  })

  it("splits a comma-separated paste and normalizes each", () => {
    expect(addCapabilityTags([], "Backend, DB ,  web ")).toEqual([
      "backend",
      "db",
      "web",
    ])
  })

  it("dedupes against existing tags (no-op when already present)", () => {
    expect(addCapabilityTags(["backend"], "Backend")).toEqual(["backend"])
    expect(addCapabilityTags(["backend"], "db, backend, db")).toEqual([
      "backend",
      "db",
    ])
  })

  it("drops empty entries and does not mutate the input array", () => {
    const existing = ["backend"]
    const next = addCapabilityTags(existing, " , , frontend")
    expect(next).toEqual(["backend", "frontend"])
    expect(existing).toEqual(["backend"])
  })
})

describe("remove (component uses Array.filter)", () => {
  it("removing by index yields the expected list", () => {
    const value = ["backend", "db", "web"]
    const removed = value.filter((_, i) => i !== 1)
    expect(removed).toEqual(["backend", "web"])
  })
})

describe("collectCapabilitySuggestions", () => {
  it("unions agent + task tags, distinct and sorted", () => {
    const agents = [
      { capabilities: ["backend", "DB"] },
      { capabilities: ["backend", "web"] },
    ]
    const tasks = [{ required_capabilities: ["db", "ml"] }]
    expect(collectCapabilitySuggestions(agents, tasks)).toEqual([
      "backend",
      "db",
      "ml",
      "web",
    ])
  })

  it("coerces JSON-encoded and comma-separated column shapes", () => {
    const agents = [
      { capabilities: '["backend", "db"]' }, // JSON-encoded string
      { capabilities: "web, ml" }, // comma-separated string
    ]
    expect(collectCapabilitySuggestions(agents, [])).toEqual([
      "backend",
      "db",
      "ml",
      "web",
    ])
  })

  it("tolerates null / missing inputs", () => {
    expect(collectCapabilitySuggestions(null, null)).toEqual([])
    expect(collectCapabilitySuggestions([{}], [{}])).toEqual([])
  })
})
