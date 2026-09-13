/**
 * Regression guard: every `@import` in the dashboard's CSS resolves to
 * a real npm dependency.
 *
 * Found live in production 2026-09-02: PR #759 (removing the System
 * page's `vis-network` dependency) deleted the graph COMPONENT files
 * and the `vis-network` line from `package.json`, but missed a
 * separate `@import "vis-network/styles/vis-network.css";` in
 * `app/globals.css`. Next.js/webpack treated the now-unresolvable
 * import as a non-fatal warning — the build still printed "Compiled
 * successfully" and exited 0 — but silently emitted a completely EMPTY
 * production CSS bundle (0 bytes) instead of failing loudly. The
 * result: every page on the live, internet-exposed dashboard rendered
 * as unstyled plain HTML, and nothing in CI caught it because `npx tsc
 * --noEmit` / `npx vitest run` never actually run a production `next
 * build` end-to-end, and the prior grep sweep for stray "vis-network"
 * references before that PR only scanned `.tsx`/`.ts`/`.json` files,
 * not `.css`.
 *
 * This test parses `app/globals.css`'s `@import` statements naming a
 * package (not a relative path) and confirms each package is declared
 * in `package.json`'s dependencies/devDependencies — the class of bug
 * this pins, not just this one instance.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const GLOBALS_CSS = resolve(DASHBOARD_ROOT, "app", "globals.css")
const PACKAGE_JSON = resolve(DASHBOARD_ROOT, "package.json")

// Matches `@import "some-package/path/file.css";` — a bare package
// specifier, not a relative (`./`, `../`) or absolute (`/`) path, and
// not a CSS at-rule name like "tailwindcss" module resolution handles
// specially (that one IS a real package too, so it's still checked).
const PACKAGE_IMPORT_RE = /@import\s+"([^./][^"]*)"\s*;/g

/** The npm package name from an import specifier, stripping any
 * subpath (`vis-network/styles/vis-network.css` -> `vis-network`,
 * `@scope/pkg/sub` -> `@scope/pkg`). */
function packageName(specifier: string): string {
  const parts = specifier.split("/")
  if (specifier.startsWith("@")) {
    return parts.slice(0, 2).join("/")
  }
  return parts[0]!
}

describe("globals.css @imports resolve to a declared dependency", () => {
  it("every package-specifier @import in globals.css is declared in package.json", () => {
    const css = readFileSync(GLOBALS_CSS, "utf8")
    const packageJson = JSON.parse(readFileSync(PACKAGE_JSON, "utf8"))
    const declared = new Set<string>([
      ...Object.keys(packageJson.dependencies ?? {}),
      ...Object.keys(packageJson.devDependencies ?? {}),
    ])

    const imports = [...css.matchAll(PACKAGE_IMPORT_RE)].map((m) => m[1]!)
    expect(
      imports.length,
      'expected at least one `@import "pkg";` in globals.css ' +
        "(e.g. tailwindcss) — the derivation is broken if this is empty",
    ).toBeGreaterThan(0)

    const missing = imports.filter((specifier) => !declared.has(packageName(specifier)))
    expect(
      missing,
      "globals.css imports a package not declared in package.json's " +
        "dependencies/devDependencies — this is EXACTLY the bug class " +
        "that shipped an empty production CSS bundle to the live " +
        "dashboard (webpack treats an unresolvable CSS @import as a " +
        "non-fatal warning, not a build failure):\n  " +
        missing.join("\n  "),
    ).toEqual([])
  })
})
