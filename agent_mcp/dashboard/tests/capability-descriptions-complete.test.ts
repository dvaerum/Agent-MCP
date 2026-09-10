/**
 * Wave 9 PR 5 — capability descriptions registry validation.
 *
 * The dashboard surfaces capabilities (the grouped checklist under
 * each group on the groups dashboard) via a description registry at
 * ``agent_mcp/dashboard/lib/capability-descriptions.ts``. The
 * canonical list of capability strings lives in
 * ``rust/conexus-core/src/capability.rs`` — the ``Capability`` enum's
 * ``as_str()`` method (Phase F, prancy-napping-pie: this test used to
 * parse ``agent_mcp/core/capabilities.py``'s ``KNOWN_CAPABILITIES``
 * frozenset, deleted along with the rest of the Python source tree —
 * ``Capability``/``Capability::ALL`` is its direct, already-existing
 * Rust replacement, "the Rust equivalent of KNOWN_CAPABILITIES" per
 * that enum's own doc comment).
 *
 * This test enforces two-way completeness:
 *
 *  1. **Every cap in the Rust ``Capability`` enum has a description.**
 *     Adding a new cap to the backend without a description means the
 *     dashboard shows an empty tooltip — fails CI.
 *
 *  2. **Every key in the description registry exists in the Rust
 *     enum.**  Stale entries (cap removed from the backend,
 *     description forgotten) clutter the dashboard list with phantom
 *     capabilities that the resolver never grants — fails CI.
 *
 * Implementation: parse the Rust source for ``as_str()``'s match arms
 * (one ``Capability::Variant => "dotted.string",`` per line) rather
 * than shelling out to `cargo`/a real Rust toolchain. Keeps the
 * dashboard test suite self-contained — no Rust dep, no subprocess,
 * no test fixtures — and the parse is robust to comment lines and
 * leading whitespace because we only accept matches inside
 * double-quoted strings on a ``=>`` line.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { CAPABILITY_DESCRIPTIONS } from "@/lib/capability-descriptions"

// Resolve relative to this test file so the test runs identically
// from the dashboard dir, repo root, or CI cwd. The Rust source lives
// at <repo>/rust/conexus-core/src/capability.rs, five levels up from
// this file: agent_mcp/dashboard/tests/<this>.test.ts.
const DASHBOARD_ROOT = resolve(__dirname, "..")
const CAPABILITY_RS = resolve(
  DASHBOARD_ROOT,
  "..",
  "..",
  "rust",
  "conexus-core",
  "src",
  "capability.rs",
)

function parseKnownCapabilities(): Set<string> {
  const src = readFileSync(CAPABILITY_RS, "utf8")
  // Find the `as_str()` match body. Match from `fn as_str` up to the
  // closing `}` of the match block (the next line starting with a
  // lone `}` at the same indent as `match self {`).
  const match = src.match(
    /fn as_str\(&self\) -> &'static str \{\s*match self \{([\s\S]*?)\n\s*\}\s*\n\s*\}/m,
  )
  if (!match) {
    throw new Error(
      "could not locate Capability::as_str()'s match body in " +
        CAPABILITY_RS,
    )
  }
  const body = match[1]!
  // Each arm's RHS is a double-quoted string after `=>`. We want only
  // those, not the `Capability::Variant` LHS.
  const caps = [...body.matchAll(/=>\s*"([^"]+)"/g)].map((m) => m[1]!)
  if (caps.length === 0) {
    throw new Error(
      "parsed zero capabilities from Capability::as_str() — regex broke?",
    )
  }
  return new Set(caps)
}

describe("capability descriptions registry", () => {
  const known = parseKnownCapabilities()
  const described = new Set(Object.keys(CAPABILITY_DESCRIPTIONS))

  it("covers every member of the Rust Capability enum", () => {
    const missing = [...known].filter((cap) => !described.has(cap)).sort()
    expect(
      missing,
      `Capability descriptions registry is missing entries for ` +
        `${missing.length} cap(s) present in Capability::as_str():\n  ` +
        missing.join("\n  ") +
        "\nAdd a one-line description in " +
        "agent_mcp/dashboard/lib/capability-descriptions.ts.",
    ).toEqual([])
  })

  it("has no orphan entries (every key is in the Rust Capability enum)", () => {
    const orphans = [...described].filter((cap) => !known.has(cap)).sort()
    expect(
      orphans,
      `Capability descriptions registry has ${orphans.length} orphan ` +
        `entr${orphans.length === 1 ? "y" : "ies"} not present in ` +
        `Capability::as_str():\n  ` +
        orphans.join("\n  ") +
        "\nEither add the cap to the Capability enum in " +
        "rust/conexus-core/src/capability.rs, or drop the description.",
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
