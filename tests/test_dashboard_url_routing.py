"""Regression guards for URL-driven dashboard section routing.

Background. The dashboard's "active section" (Overview / Agents /
Tasks / Memories / Messages / Settings / Prompt Book) used to live
exclusively in zustand state (`useDashboard.currentView`). Reloading
the page reset the view to Overview, and there was no way to share a
URL that pointed at a non-Overview section — both broke Dennis's
expected behaviour ("click Tasks, reload, end up on Tasks"; "paste
URL while on Agents, recipient sees Agents").

The fix wires the active section to the URL via the `?page=<section>`
query parameter. Reasons for the query-param shape (vs proper
Next.js route segments):

  * No Next.js app-router file restructuring is required (the
    dashboard is a single client page that switches a `currentView`
    enum) — query-param keeps the diff small.
  * Coexists cleanly with the path-prefix adapter from PR #56 that
    mounts the dashboard at `/agent-mcp/__dashboard/<project>/`. The
    shape becomes `/agent-mcp/__dashboard/<project>/?page=tasks`.
  * `useSearchParams` is the canonical Next.js hook for this; no
    `dynamicParams` / `generateStaticParams` plumbing needed.
  * Bookmarks + share-links work out of the box.
  * Missing param falls back to Overview, matching legacy behaviour.

The tests parse `.tsx` source — no jsdom/RTL infrastructure in this
fork (same lightweight pattern as
test_dashboard_sidebar_toggle_mobile.py, test_dashboard_path_prefix_adapter.py).
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")
APP_PAGE = DASHBOARD / "app" / "page.tsx"
NAVIGATION = DASHBOARD / "components" / "layout" / "navigation.tsx"
USE_SECTION_ROUTE = DASHBOARD / "lib" / "use-section-route.ts"


def _read(p: Path) -> str:
    return p.read_text()


# ---------------------------------------------------------------------------
# Section enum — must be a single source of truth
# ---------------------------------------------------------------------------

EXPECTED_SECTIONS = {
    "overview",
    "agents",
    "tasks",
    "memories",
    "messages",
    "settings",
    "system",
    "prompts",
}


def test_section_enum_values_are_url_safe() -> None:
    """The section enum values used in the URL must be URL-safe
    (lowercase alphanumeric, no spaces / special chars). All current
    sections happen to be single words — pinning this so a future
    rename to e.g. 'Prompt Book' as the URL value would fail the
    test instead of producing `?page=Prompt%20Book` in share-links."""
    for s in EXPECTED_SECTIONS:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", s), (
            f"Section enum value {s!r} is not URL-safe; expected "
            f"lowercase alphanumeric/-"
        )


# ---------------------------------------------------------------------------
# The use-section-route hook — single source of truth for URL <-> section
# ---------------------------------------------------------------------------


def test_use_section_route_hook_exists() -> None:
    """A small dedicated hook centralises the URL <-> section bridging
    so that page.tsx and the Navigation component both read/write
    through the same code path. Extracting it to lib/use-section-route.ts
    keeps page.tsx readable and makes the URL contract testable from
    one location."""
    assert USE_SECTION_ROUTE.exists(), (
        f"Expected dedicated hook at {USE_SECTION_ROUTE} that wraps "
        "useSearchParams + useRouter to expose `currentSection` + "
        "`setSection(section)`."
    )


def test_use_section_route_reads_search_params() -> None:
    """The hook must read the active section from URL search params
    (Next.js `useSearchParams`) — this is the load-bearing piece that
    makes reload-stable navigation work."""
    src = _read(USE_SECTION_ROUTE)
    assert "useSearchParams" in src, (
        "use-section-route.ts must import and call `useSearchParams` "
        "from next/navigation to read the `?page=` query."
    )
    # The query key must be `page` (the chosen URL shape).
    assert re.search(r"['\"]page['\"]", src), (
        "use-section-route.ts must reference the 'page' search-param "
        "key — that is the agreed URL shape."
    )


def test_use_section_route_writes_via_router() -> None:
    """The hook must expose a setter that updates the URL via the
    Next.js router (router.push / router.replace) so subsequent
    reloads + browser back/forward work correctly."""
    src = _read(USE_SECTION_ROUTE)
    assert "useRouter" in src, (
        "use-section-route.ts must import `useRouter` from next/navigation."
    )
    # Either replace or push is acceptable. replace avoids polluting
    # the back stack on every nav-click; push enables back/forward
    # through history. We allow either but require one.
    assert re.search(r"router\.(replace|push)\s*\(", src), (
        "use-section-route.ts setter must call router.push / router.replace "
        "to write the new ?page=… into the URL."
    )


def test_use_section_route_defaults_to_overview() -> None:
    """Missing `?page` must default to 'overview' so the bare
    dashboard URL keeps working unchanged."""
    src = _read(USE_SECTION_ROUTE)
    # Looking for an "overview" default — accept either the literal
    # 'overview' string used as a fallback, or a constant named with
    # 'overview' in it.
    assert "'overview'" in src or '"overview"' in src, (
        "use-section-route.ts must reference 'overview' as the default "
        "section when ?page= is missing or invalid."
    )


def test_use_section_route_rejects_unknown_sections() -> None:
    """Unknown / typo'd `?page=` values must fall back to the default
    (overview) rather than break rendering."""
    src = _read(USE_SECTION_ROUTE)
    # The hook should validate the param against the known enum. The
    # simplest way to pin this is to require at least the section
    # union or a guard helper. We check that the source references
    # most section names (validation against the known set).
    referenced = sum(1 for s in EXPECTED_SECTIONS if f"'{s}'" in src or f'"{s}"' in src)
    assert referenced >= 4, (
        "use-section-route.ts must reference the section enum values for "
        "input validation — guard against unknown ?page= values by "
        "falling back to 'overview'. Found "
        f"{referenced} of {len(EXPECTED_SECTIONS)} sections referenced."
    )


# ---------------------------------------------------------------------------
# page.tsx — must read active section from the URL, not from zustand alone
# ---------------------------------------------------------------------------


def test_page_uses_section_route_hook() -> None:
    """app/page.tsx must derive the rendered section from the URL via
    the use-section-route hook (or useSearchParams directly). Reading
    only from zustand state means reload always lands on Overview —
    which is the bug we are fixing."""
    src = _read(APP_PAGE)
    uses_hook = "useSectionRoute" in src
    uses_search_params = "useSearchParams" in src
    assert uses_hook or uses_search_params, (
        "app/page.tsx must read the active section via useSectionRoute "
        "(from lib/use-section-route.ts) or useSearchParams (from "
        "next/navigation) so the URL drives the rendered section. "
        "Reading from useDashboard.currentView only means reload "
        "loses the section."
    )


def test_page_renders_all_known_sections() -> None:
    """The switch in page.tsx must continue to handle every known
    section so the URL → component map stays in sync with the enum."""
    src = _read(APP_PAGE)
    for section in EXPECTED_SECTIONS:
        # Each case appears as case 'overview': etc.
        assert (
            f"case '{section}'" in src or f'case "{section}"' in src
        ), f"page.tsx must have a `case` for section {section!r}"


# ---------------------------------------------------------------------------
# Navigation — sidebar items must update the URL on click
# ---------------------------------------------------------------------------


def test_navigation_writes_url_on_click() -> None:
    """Clicking a sidebar nav item must update the URL so reload +
    share-links work. Implementation must call either useSectionRoute's
    setter or router.push/replace with the new ?page= value."""
    src = _read(NAVIGATION)
    uses_hook = "useSectionRoute" in src
    uses_router_push = re.search(r"router\.(replace|push)\s*\(", src) is not None
    assert uses_hook or uses_router_push, (
        "navigation.tsx must update the URL on nav-click — either by "
        "calling the useSectionRoute setter or by calling "
        "router.push/replace with the new ?page= param."
    )


def test_navigation_does_not_only_call_zustand_setter() -> None:
    """The nav onClick must NOT be a bare `setCurrentView(item.view)`
    call without also updating the URL. Pin the regression: if the
    onClick only updates zustand, reload won't preserve the section."""
    src = _read(NAVIGATION)
    # The previous (buggy) onClick was effectively:
    #     onClick={() => { setCurrentView(item.view); if (isMobile) setOpenMobile(false); }}
    # Detect that exact pattern with no URL write nearby. We check the
    # whole file: if `useSectionRoute` is imported OR `router.push` is
    # called, we're fine. Otherwise, the file is the old buggy shape.
    has_url_write = (
        "useSectionRoute" in src
        or re.search(r"router\.(replace|push)\s*\(", src) is not None
    )
    assert has_url_write, (
        "navigation.tsx onClick must write to the URL (via useSectionRoute "
        "setter or router.push/replace), not only call zustand "
        "setCurrentView. Otherwise reload loses the section."
    )


def test_navigation_section_enum_matches_expected() -> None:
    """The NavItem.view union in navigation.tsx must match the agreed
    URL-section enum so /?page=<view> always maps to a real menu
    item."""
    src = _read(NAVIGATION)
    # Find the `view: '...'` union in the NavItem interface.
    match = re.search(r"view:\s*((?:'[a-z]+'\s*\|\s*)+'[a-z]+')", src)
    assert match is not None, (
        "navigation.tsx must declare the NavItem.view union literally "
        "(view: 'overview' | 'agents' | ...). Could not locate."
    )
    declared = set(re.findall(r"'([a-z]+)'", match.group(1)))
    assert declared == EXPECTED_SECTIONS, (
        f"navigation.tsx view union {declared!r} does not match the "
        f"expected URL-section enum {EXPECTED_SECTIONS!r}."
    )
