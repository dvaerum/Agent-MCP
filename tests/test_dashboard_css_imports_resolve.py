"""Regression guard: every `@import` in the dashboard's CSS resolves to
a real npm dependency.

Found live in production 2026-09-02: PR #759 (removing the System
page's `vis-network` dependency) deleted the graph COMPONENT files and
the `vis-network` line from `package.json`, but missed a separate
`@import "vis-network/styles/vis-network.css";` in `app/globals.css`.
Next.js/webpack treated the now-unresolvable import as a non-fatal
warning — the build still printed "Compiled successfully" and exited
0 — but silently emitted a completely EMPTY production CSS bundle (0
bytes) instead of failing loudly. The result: every page on the live,
internet-exposed dashboard rendered as unstyled plain HTML, and
nothing in CI caught it because `npx tsc --noEmit` / `npx vitest run`
never actually run a production `next build` end-to-end, and the
prior grep sweep for stray "vis-network" references before that PR
only scanned `.tsx`/`.ts`/`.json` files, not `.css`.

This test parses `app/globals.css`'s `@import` statements naming a
package (not a relative path) and confirms each package is declared
in `package.json`'s dependencies/devDependencies — the class of bug
this pins, not just this one instance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
GLOBALS_CSS = DASHBOARD / "app" / "globals.css"
PACKAGE_JSON = DASHBOARD / "package.json"

# Matches `@import "some-package/path/file.css";` — a bare package
# specifier, not a relative (`./`, `../`) or absolute (`/`) path, and
# not a CSS at-rule name like "tailwindcss" module resolution handles
# specially (that one IS a real package too, so it's still checked).
_PACKAGE_IMPORT_RE = re.compile(r'@import\s+"([^./][^"]*)"\s*;')


def _package_name(specifier: str) -> str:
    """The npm package name from an import specifier, stripping any
    subpath (`vis-network/styles/vis-network.css` -> `vis-network`,
    `@scope/pkg/sub` -> `@scope/pkg`)."""
    parts = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def test_globals_css_imports_all_resolve_to_a_declared_dependency() -> None:
    css = GLOBALS_CSS.read_text()
    package_json = json.loads(PACKAGE_JSON.read_text())
    declared = {
        *package_json.get("dependencies", {}).keys(),
        *package_json.get("devDependencies", {}).keys(),
    }

    imports = _PACKAGE_IMPORT_RE.findall(css)
    assert imports, (
        "expected at least one `@import \"pkg\";` in globals.css "
        "(e.g. tailwindcss) — the derivation is broken if this is empty"
    )

    missing = [
        specifier
        for specifier in imports
        if _package_name(specifier) not in declared
    ]
    assert not missing, (
        "globals.css imports a package not declared in package.json's "
        "dependencies/devDependencies — this is EXACTLY the bug class "
        "that shipped an empty production CSS bundle to the live "
        "dashboard (webpack treats an unresolvable CSS @import as a "
        "non-fatal warning, not a build failure):\n  "
        + "\n  ".join(missing)
    )
