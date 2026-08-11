/**
 * Wave 9 PR 5 — capability descriptions registry validation.
 *
 * The dashboard surfaces capabilities (the grouped checklist under
 * each group on the groups dashboard) via a description registry at
 * ``agent_mcp/dashboard/lib/capability-descriptions.ts``. The
 * canonical list of capability strings lives in
 * ``agent_mcp/core/capabilities.py`` — ``KNOWN_CAPABILITIES``.
 *
 * This test enforces two-way completeness:
 *
 *  1. **Every cap in ``KNOWN_CAPABILITIES`` has a description.**
 *     Adding a new cap to the backend without a description means the
 *     dashboard shows an empty tooltip — fails CI.
 *
 *  2. **Every key in the description registry exists in
 *     ``KNOWN_CAPABILITIES``.**  Stale entries (cap removed from the
 *     backend, description forgotten) clutter the dashboard list with
 *     phantom capabilities that the resolver never grants — fails CI.
 *
 * Implementation: parse the Python source for ``KNOWN_CAPABILITIES``
 * (a frozenset literal with one cap-string per line) rather than
 * shelling out to Python. Keeps the dashboard test suite self-contained
 * — no Python dep, no subprocess, no test fixtures — and the parse
 * is robust to comment lines and leading whitespace because we only
 * accept matches inside double-quoted strings.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { CAPABILITY_DESCRIPTIONS } from "@/lib/capability-descriptions"

// Resolve relative to this test file so the test runs identically
// from the dashboard dir, repo root, or CI cwd. The Python source
// lives at <repo>/agent_mcp/core/capabilities.py, four levels up
// from this file: agent_mcp/dashboard/tests/<this>.test.ts.
const DASHBOARD_ROOT = resolve(__dirname, "..")
const CAPABILITIES_PY = resolve(
  DASHBOARD_ROOT,
  "..",
  "core",
  "capabilities.py",
)

function parseKnownCapabilities(): Set<string> {
  const src = readFileSync(CAPABILITIES_PY, "utf8")
  // Find the KNOWN_CAPABILITIES frozenset literal. Match from
  // ``KNOWN_CAPABILITIES`` up to the closing ``})``. The body is a
  // multi-line ``frozenset({...})`` with one cap-string per line.
  const match = src.match(
    /KNOWN_CAPABILITIES\s*:\s*frozenset\[str\]\s*=\s*frozenset\(\{([\s\S]*?)\}\)/m,
  )
  if (!match) {
    throw new Error(
      "could not locate KNOWN_CAPABILITIES literal in " + CAPABILITIES_PY,
    )
  }
  const body = match[1]!
  // Each cap is a double-quoted string. Comments may appear; we want
  // only the quoted strings.
  const caps = [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1]!)
  if (caps.length === 0) {
    throw new Error(
      "parsed zero capabilities from KNOWN_CAPABILITIES — regex broke?",
    )
  }
  return new Set(caps)
}

describe("capability descriptions registry", () => {
  const known = parseKnownCapabilities()
  const described = new Set(Object.keys(CAPABILITY_DESCRIPTIONS))

  it("covers every member of KNOWN_CAPABILITIES", () => {
    const missing = [...known].filter((cap) => !described.has(cap)).sort()
    expect(
      missing,
      `Capability descriptions registry is missing entries for ` +
        `${missing.length} cap(s) present in KNOWN_CAPABILITIES:\n  ` +
        missing.join("\n  ") +
        "\nAdd a one-line description in " +
        "agent_mcp/dashboard/lib/capability-descriptions.ts.",
    ).toEqual([])
  })

  it("has no orphan entries (every key is in KNOWN_CAPABILITIES)", () => {
    const orphans = [...described].filter((cap) => !known.has(cap)).sort()
    expect(
      orphans,
      `Capability descriptions registry has ${orphans.length} orphan ` +
        `entr${orphans.length === 1 ? "y" : "ies"} not present in ` +
        `KNOWN_CAPABILITIES:\n  ` +
        orphans.join("\n  ") +
        "\nEither add the cap to KNOWN_CAPABILITIES in " +
        "agent_mcp/core/capabilities.py, or drop the description.",
    ).toEqual([])
  })

  it("every description is a non-empty single-line string", () => {
    const offenders: string[] = []
    for (const [cap, desc] of Object.entries(CAPABILITY_DESCRIPTIONS)) {
      if (!desc || desc.trim().length === 0) {
        offenders.push(`${cap}: empty`)
        continue
      }
      if (desc.includes("\n")) {
        offenders.push(`${cap}: multi-line`)
        continue
      }
    }
    expect(
      offenders,
      `Capability description policy violated by ${offenders.length} entr` +
        `${offenders.length === 1 ? "y" : "ies"}:\n  ` +
        offenders.join("\n  "),
    ).toEqual([])
  })
})
