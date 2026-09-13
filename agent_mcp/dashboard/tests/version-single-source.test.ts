/**
 * Regression guards: `package.json` is the single source of truth for
 * the product version shown in the dashboard UI.
 *
 * Historically four copies drifted apart: the Python package's
 * `__init__.__version__` froze at "2.2.0", the dashboard sidebar
 * hardcoded "v3.4.0", the dashboard `package.json` sat at the Next.js
 * scaffold default, and git tags stalled a scheme behind
 * `pyproject.toml` (5.0.71).
 *
 * `pyproject.toml` was the single source of truth while the Python
 * implementation existed; once it was deleted wholesale (Phase F,
 * prancy-napping-pie), `package.json`'s own `version` field took over
 * that role -- `next.config.ts`'s `resolveVersion()` reads it directly,
 * and `nix/packages.nix`'s `version` let-binding reads the exact same
 * file for the sandboxed Nix build. These tests fail the moment any
 * consumer re-hardcodes a literal instead of deriving from it.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const PACKAGE_JSON = resolve(DASHBOARD_ROOT, "package.json")
const SIDEBAR = resolve(DASHBOARD_ROOT, "components", "layout", "app-sidebar.tsx")
const NEXT_CONFIG = resolve(DASHBOARD_ROOT, "next.config.ts")

function packageVersion(): string {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON, "utf8"))
  return pkg.version
}

describe("version single source of truth", () => {
  it("package.json has a real semver version, not a placeholder", () => {
    const version = packageVersion()
    expect(version).toMatch(/^\d+\.\d+\.\d+/)
    expect(
      version,
      "package.json's version looks like the Next.js scaffold default " +
        "(0.1.0) rather than a real product version -- bump it to match " +
        "the current release.",
    ).not.toBe("0.1.0")
  })

  it("sidebar has no hardcoded version literal", () => {
    const src = readFileSync(SIDEBAR, "utf8")
    // No `v1.2.3`-style literal baked into the component (it was "v3.4.0").
    const hardcoded = src.match(/v\d+\.\d+\.\d+/)
    expect(
      hardcoded,
      `hardcoded version literal ${JSON.stringify(
        hardcoded?.[0],
      )} found in app-sidebar.tsx; derive it from NEXT_PUBLIC_AGENT_MCP_VERSION instead`,
    ).toBeNull()
    // ...and it must actually read the derived env var.
    expect(src).toContain("NEXT_PUBLIC_AGENT_MCP_VERSION")
  })

  it("next.config.ts wires the version env from package.json", () => {
    // What makes the version reach the client bundle (env-var first,
    // package.json fallback for plain `npm run dev`).
    const src = readFileSync(NEXT_CONFIG, "utf8")
    expect(src).toContain("NEXT_PUBLIC_AGENT_MCP_VERSION")
    expect(
      src,
      "next.config.ts should resolve its package.json fallback " +
        "relative to this directory (not a repo-root pyproject.toml, " +
        "retired along with the rest of the Python implementation).",
    ).toMatch(/readFileSync\(join\(__dirname,\s*["']package\.json["']\)/)
  })
})
