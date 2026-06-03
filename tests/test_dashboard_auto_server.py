"""Regression guards for the dashboard path-prefix derivation.

Originally these guarded the 3-useEffect bootstrap in
`api-client-initializer.tsx` (auto-seed + cold-start retry +
hydration-gate). Candidate C from the 2026-06-01 architecture review
collapsed that into:

  - A module-level singleton in `lib/project-context.ts` that derives
    `{projectName, baseUrl, apiPrefix}` synchronously from
    `window.location.pathname` at import time (no useEffect, no
    zustand-persist hydration race).
  - Transparent retry inside `ApiClient.request()` on 502/503/504
    (no boundary-level setInterval poll).

These guards now check the new module. The "cold-start retry" guard
moved to `test_dashboard_path_prefix_adapter.py` (asserting the retry
loop in `lib/api.ts`), and the "hydration-gate" guard is gone — the
new derivation is synchronous and cannot race the persisted state.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_CONTEXT = Path("agent_mcp/dashboard/lib/project-context.ts")


def _src() -> str:
    return PROJECT_CONTEXT.read_text()


def test_path_prefix_derivation_uses_dashboard_path_regex() -> None:
    """The PathPrefix singleton must inspect window.location.pathname
    for the deployment URL pattern so the dashboard self-bootstraps
    when mounted under /agent-mcp/app/<name>/ (PR-B renamed from
    /__dashboard/). The regex literal moved to lib/urls.ts (PR-B
    centralisation); project-context.ts imports the matcher."""
    src = _src()
    assert "APP_PROJECT_PATH_RE" in src, (
        "expected project-context.ts to import APP_PROJECT_PATH_RE "
        "from lib/urls.ts (PR-B centralisation)"
    )
    urls_src = Path("agent_mcp/dashboard/lib/urls.ts").read_text()
    assert "/agent-mcp/app" in urls_src, (
        "expected the path-prefix regex `/agent-mcp/app` in lib/urls.ts; "
        "derivation only works when the deployment URL pattern is detected"
    )
    assert "window.location.pathname" in src, (
        "expected the singleton to read window.location.pathname "
        "(synchronous derivation at module import)"
    )


def test_path_prefix_derivation_has_ssr_guard() -> None:
    """Next.js prerenders the module at build time where `window` is
    undefined. The singleton must guard with `typeof window` to fall
    through to defaults during SSR."""
    src = _src()
    assert "typeof window" in src, (
        "expected `typeof window !== 'undefined'` SSR guard so the "
        "module imports cleanly during Next.js prerender"
    )
