/**
 * Settings dashboard — data-driven rendering (ADR-0018, PR 2).
 *
 * The Settings page renders itself from the backend schema registry
 * (GET /api/settings-schema) via a type→widget registry. This test
 * pins the three properties that make that data-driven rendering
 * correct:
 *
 *   1. type→widget mapping — every `widget` (and the `type` fallback)
 *      resolves to the right control kind, and each kind renders the
 *      right DOM control.
 *   2. group ordering — the five groups render in the fixed order
 *      (worker_permissions → event_loop → scheduling → agent_profiles →
 *      retention), empty groups dropped, schema order preserved within a
 *      group.
 *   3. tier-gating — a sysadmin-tier entry renders DISABLED (with the
 *      inline note) when the caller is not a sysadmin, and enabled
 *      when they are (fixes the silent-403 mis-tier).
 *
 * Uses `React.createElement` + `renderToStaticMarkup` (no jsdom/RTL,
 * matching tests/memory-value-xss.test.ts) so this stays a pure-Node
 * `.ts` test that the vitest react transform handles.
 */

import React from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { SettingsSchemaEntry } from "@/lib/api"
import {
  GROUP_ORDER,
  groupSchema,
  isTierLocked,
  widgetKindFor,
  SettingControl,
  secondsToParts,
  partsToSeconds,
  formatDuration,
  type WidgetKind,
} from "@/components/dashboard/settings-dashboard"

function entry(over: Partial<SettingsSchemaEntry>): SettingsSchemaEntry {
  return {
    key: "config_example",
    type: "bool",
    default: false,
    tier: "operator",
    group: "worker_permissions",
    title: "Example",
    description: "An example setting.",
    widget: "switch",
    ...over,
  }
}

// Render a single control to static HTML for DOM assertions.
function renderControl(
  e: SettingsSchemaEntry,
  opts: { locked?: boolean; exists?: boolean } = {},
): string {
  const kind = widgetKindFor(e)
  return renderToStaticMarkup(
    React.createElement(SettingControl, {
      entry: e,
      kind,
      locked: opts.locked ?? false,
      row: undefined,
      boolRow:
        kind === "switch"
          ? { value: Boolean(e.default), exists: opts.exists ?? false, pending: false }
          : undefined,
      draft: undefined,
      touched: false,
      pending: false,
      exists: opts.exists ?? false,
      onToggle: () => {},
      onDraft: () => {},
      onBlur: () => {},
      onSave: () => {},
    }),
  )
}

describe("widgetKindFor — type→widget mapping", () => {
  const cases: Array<[SettingsSchemaEntry["widget"], WidgetKind]> = [
    ["switch", "switch"],
    ["int_days", "int_days"],
    ["int_ms", "int_ms"],
    ["int_duration", "int_duration"],
    ["url", "text"],
    ["template", "text"],
    ["secret", "secret"],
    ["secret_path", "secret"],
  ]
  for (const [widget, kind] of cases) {
    it(`maps widget "${widget}" → "${kind}"`, () => {
      expect(widgetKindFor(entry({ widget }))).toBe(kind)
    })
  }

  it("falls back to `type` when the widget hint is unknown", () => {
    // Cast: exercise the runtime fallback for an unrecognised widget.
    const weird = { widget: "mystery" } as unknown as Partial<SettingsSchemaEntry>
    expect(widgetKindFor(entry({ ...weird, type: "bool" }))).toBe("switch")
    expect(widgetKindFor(entry({ ...weird, type: "int" }))).toBe("int_ms")
    expect(widgetKindFor(entry({ ...weird, type: "secret" }))).toBe("secret")
    expect(widgetKindFor(entry({ ...weird, type: "string" }))).toBe("text")
  })
})

describe("SettingControl — each kind renders the right control", () => {
  it("switch → a role=switch control (not a text/number/password field)", () => {
    const html = renderControl(entry({ type: "bool", widget: "switch" }))
    expect(html).toMatch(/role="switch"/)
    // Radix renders a hidden checkbox companion; assert it is not one
    // of the editable field types the other widgets use.
    expect(html).not.toMatch(/type="(text|number|password)"/)
  })

  it("int_days → a number input plus a 'days' label", () => {
    const html = renderControl(
      entry({ type: "int", widget: "int_days", default: 0 }),
    )
    expect(html).toMatch(/<input[^>]*type="number"/)
    expect(html).toContain("days")
  })

  it("int_ms → a number input with no 'days' label", () => {
    const html = renderControl(
      entry({ type: "int", widget: "int_ms", default: 2000 }),
    )
    expect(html).toMatch(/<input[^>]*type="number"/)
    expect(html).not.toContain("days")
  })

  it("int_duration → a number input + a unit select (minutes/hours/days)", () => {
    const html = renderControl(
      entry({ type: "int", widget: "int_duration", default: 604800 }),
    )
    expect(html).toMatch(/<input[^>]*type="number"/)
    expect(html).toMatch(/<select/)
    expect(html).toContain("minutes")
    expect(html).toContain("hours")
    expect(html).toContain("days")
    // 604800s = 7 days: the amount input seeds to 7 and unit to days.
    expect(html).toMatch(/value="7"/)
  })

  it("url → a text input", () => {
    const html = renderControl(entry({ type: "string", widget: "url" }))
    expect(html).toMatch(/<input[^>]*type="text"/)
  })

  it("template → a text input", () => {
    const html = renderControl(entry({ type: "string", widget: "template" }))
    expect(html).toMatch(/<input[^>]*type="text"/)
  })

  it("secret → a write-only password input (never a text field)", () => {
    const html = renderControl(entry({ type: "secret", widget: "secret" }))
    expect(html).toMatch(/<input[^>]*type="password"/)
  })
})

describe("int_duration — seconds ⇄ {amount, unit} conversion", () => {
  it("secondsToParts picks the largest whole unit", () => {
    expect(secondsToParts(604800)).toEqual({ amount: 7, unit: "days" })
    expect(secondsToParts(3600)).toEqual({ amount: 1, unit: "hours" })
    expect(secondsToParts(120)).toEqual({ amount: 2, unit: "minutes" })
    // 90 min = 5400s: not a whole hour/day → minutes.
    expect(secondsToParts(5400)).toEqual({ amount: 90, unit: "minutes" })
  })

  it("secondsToParts treats 0/negative as 0 days", () => {
    expect(secondsToParts(0)).toEqual({ amount: 0, unit: "days" })
    expect(secondsToParts(-5)).toEqual({ amount: 0, unit: "days" })
  })

  it("partsToSeconds is the inverse", () => {
    expect(partsToSeconds(7, "days")).toBe(604800)
    expect(partsToSeconds(2, "hours")).toBe(7200)
    expect(partsToSeconds(30, "minutes")).toBe(1800)
    expect(partsToSeconds(-1, "days")).toBe(0)
  })

  it("formatDuration renders human copy, 0 = never stop", () => {
    expect(formatDuration(604800)).toBe("7 days")
    expect(formatDuration(0)).toBe("never stop")
  })
})

describe("group ordering", () => {
  it("GROUP_ORDER is the five groups in the specified order", () => {
    expect(GROUP_ORDER.map((g) => g.group)).toEqual([
      "worker_permissions",
      "event_loop",
      "scheduling",
      "agent_profiles",
      "retention",
    ])
    expect(GROUP_ORDER.map((g) => g.title)).toEqual([
      "Worker permissions",
      "Agent event-loop",
      "Scheduled directives",
      "Agent profiles",
      "Message retention",
    ])
  })

  it("groupSchema orders groups canonically regardless of schema order", () => {
    // Deliberately shuffled input order.
    const schema: SettingsSchemaEntry[] = [
      entry({ key: "ap1", group: "agent_profiles" }),
      entry({ key: "wp1", group: "worker_permissions" }),
      entry({ key: "ret1", group: "retention", widget: "int_days", type: "int" }),
      entry({ key: "el1", group: "event_loop" }),
    ]
    const groups = groupSchema(schema)
    expect(groups.map((g) => g.group)).toEqual([
      "worker_permissions",
      "event_loop",
      "agent_profiles",
      "retention",
    ])
  })

  it("groupSchema preserves schema order within a group and drops empty groups", () => {
    const schema: SettingsSchemaEntry[] = [
      entry({ key: "wp_b", group: "worker_permissions" }),
      entry({ key: "wp_a", group: "worker_permissions" }),
    ]
    const groups = groupSchema(schema)
    // Only the one non-empty group survives.
    expect(groups).toHaveLength(1)
    expect(groups[0].group).toBe("worker_permissions")
    expect(groups[0].entries.map((e) => e.key)).toEqual(["wp_b", "wp_a"])
  })
})

describe("tier-gating (silent-403 fix)", () => {
  const sysadminEntry = entry({
    key: "config_synthetic_sysadmin",
    tier: "sysadmin",
    group: "worker_permissions",
    widget: "switch",
    type: "bool",
  })

  it("isTierLocked: sysadmin entry locked for a non-sysadmin caller", () => {
    expect(isTierLocked(sysadminEntry, { sysadmin: false })).toBe(true)
  })

  it("isTierLocked: sysadmin entry unlocked for a sysadmin caller", () => {
    expect(isTierLocked(sysadminEntry, { sysadmin: true })).toBe(false)
  })

  it("isTierLocked: operator entry never locked", () => {
    const op = entry({ tier: "operator" })
    expect(isTierLocked(op, { sysadmin: false })).toBe(false)
    expect(isTierLocked(op, { sysadmin: true })).toBe(false)
  })

  it("renders the switch DISABLED when a non-sysadmin views a sysadmin entry", () => {
    const html = renderControl(sysadminEntry, { locked: true })
    expect(html).toMatch(/role="switch"/)
    expect(html).toMatch(/disabled=""/)
  })

  it("renders the switch ENABLED when a sysadmin views the same entry", () => {
    const html = renderControl(sysadminEntry, { locked: false })
    expect(html).toMatch(/role="switch"/)
    expect(html).not.toMatch(/disabled=""/)
  })

  it("locks a sysadmin secret field for a plain operator", () => {
    const secretEntry = entry({
      key: "config_synthetic_secret",
      tier: "sysadmin",
      group: "worker_permissions",
      widget: "secret",
      type: "secret",
    })
    const html = renderControl(secretEntry, { locked: true })
    expect(html).toMatch(/<input[^>]*type="password"/)
    expect(html).toMatch(/disabled=""/)
  })
})
