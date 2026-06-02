"""Regression-guard pins for the mobile sidebar toggle visibility bug.

Background. On narrow viewports the sidebar (rendered as a Sheet
overlay via the shadcn `<Sidebar>` primitive) ends up covering the
entire viewport, including the `<Header>` row that hosts the hamburger
toggle. Combined with two other defects this leaves users with **no
visible way to dismiss the sidebar**:

  1. `SheetContent`'s built-in close (X) button is hidden via the
     `[&>button]:hidden` selector on the shadcn `<Sidebar>` mobile
     branch (components/ui/sidebar.tsx line 190). That's an upstream
     shadcn default the dashboard inherits.

  2. AppSidebar's effect at lines 38-42 force-opens the mobile sheet
     whenever `isMobile && state === 'expanded'`. If the user closes
     the sheet but the `state` is still expanded (it's a separate
     desktop state machine), the next re-render flicks it back open.

  3. The header `<Menu>` button is rendered behind the sheet — its
     z-index (50) does not exceed the SheetContent overlay's z-index.

This file pins the structural fix:

  * The mobile sheet variant must render a visible close affordance
    that's reachable while the sheet is open (a Button rendered
    inside SidebarHeader for `isMobile`).
  * The header hamburger toggle must remain visible at every viewport
    below `lg:` (no `md:hidden`/`sm:hidden` on the trigger).
  * The trigger button must carry an accessible label
    ("Toggle navigation menu" or "Toggle sidebar") via `sr-only`
    span / `aria-label`.
  * The force-open mobile effect must NOT re-fire while the user has
    explicitly dismissed the sheet.

Tests parse .tsx source — no dashboard runtime needed (same
lightweight pattern as test_dashboard_polish_mobile_pass.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("agent_mcp/dashboard")
LAYOUT = ROOT / "components" / "layout"
HEADER = LAYOUT / "header.tsx"
APP_SIDEBAR = LAYOUT / "app-sidebar.tsx"


def _read(p: Path) -> str:
    return p.read_text()


# ---------------------------------------------------------------------------
# Header hamburger toggle — must be visible at every viewport < lg
# ---------------------------------------------------------------------------


def test_header_hamburger_visible_below_lg() -> None:
    """The header `<Menu>` button must remain visible at every viewport
    below the `lg:` breakpoint (1024 px). It is the primary
    open-sidebar affordance on tablet (768-1023 px) where the
    in-sidebar Panel toggle is also present, and it MUST exist on
    mobile (<768 px) so the user can re-open the sheet after
    dismissing it.

    Regression we are pinning: prior versions of the dashboard hid the
    button at narrower breakpoints (`md:hidden` or `sm:hidden`),
    leaving a viewport range where the only entry-point to the
    sidebar disappeared.
    """
    src = _read(HEADER)

    # The Menu icon button must exist.
    assert "Menu" in src, "header.tsx must import + render lucide Menu icon"
    assert "toggleSidebar" in src, "header.tsx must wire toggleSidebar from useSidebar"

    # Locate the Menu button's className. It should use lg:hidden
    # (visible 0-1023, hidden 1024+) and MUST NOT use md:hidden /
    # sm:hidden which would create a dead range.
    menu_block_match = re.search(
        r"<Button[^>]*onClick=\{toggleSidebar\}[^>]*?className=\{?\"([^\"]+)\"",
        src,
        re.DOTALL,
    )
    assert menu_block_match is not None, (
        "Could not locate the header Menu toggle <Button> in header.tsx; "
        "test parsing assumes onClick={toggleSidebar} + className=\"...\""
    )

    classes = menu_block_match.group(1)

    forbidden = ["md:hidden", "sm:hidden", "xl:hidden"]
    for cls in forbidden:
        assert cls not in classes, (
            f"Header hamburger toggle uses '{cls}' which would hide it at "
            f"viewports < lg; this re-introduces the bug (full classes: {classes!r})."
        )

    # Positive: lg:hidden is the correct gate.
    assert "lg:hidden" in classes, (
        "Header hamburger toggle must use lg:hidden so it's visible at "
        f"every viewport < 1024 px (current classes: {classes!r})."
    )


def test_header_hamburger_has_accessible_label() -> None:
    """The hamburger toggle must carry an accessible label (sr-only
    span or aria-label) so screen readers announce it."""
    src = _read(HEADER)

    # Look for an sr-only span near the Menu button. We match the
    # canonical shadcn pattern: <span className="sr-only">…</span>
    # somewhere inside the Menu <Button>.
    pattern = re.compile(
        r"<Button[^>]*onClick=\{toggleSidebar\}.*?</Button>",
        re.DOTALL,
    )
    btn_match = pattern.search(src)
    assert btn_match is not None, "Could not locate header Menu Button block"

    btn_body = btn_match.group(0)
    has_sr_only = 'className="sr-only"' in btn_body or "className='sr-only'" in btn_body
    has_aria = "aria-label=" in btn_body
    assert has_sr_only or has_aria, (
        "Header hamburger toggle must have a sr-only label span or aria-label "
        f"for screen-reader accessibility. Button block: {btn_body!r}"
    )


# ---------------------------------------------------------------------------
# Mobile sheet — must have a visible close button INSIDE the sheet
# ---------------------------------------------------------------------------


def test_mobile_sheet_renders_visible_close_button() -> None:
    """When the sidebar is rendered as a Sheet overlay on mobile
    (isMobile === true), there must be a visible close affordance
    INSIDE the sheet's own DOM. The header's hamburger is behind the
    sheet's z-index when the sheet is open, so without an in-sheet
    close button the user is trapped.

    Pin: app-sidebar.tsx must render a Button (with X / PanelLeftClose
    icon) gated on `isMobile` inside SidebarHeader, wired to
    setOpenMobile(false).
    """
    src = _read(APP_SIDEBAR)

    # Look for setOpenMobile being called with `false` somewhere
    # (closure or arrow), which is how the in-sheet close must
    # dismiss the sheet.
    assert re.search(r"setOpenMobile\(\s*false\s*\)", src), (
        "app-sidebar.tsx must render an in-sheet close button that calls "
        "setOpenMobile(false). The header toggle is behind the sheet on "
        "mobile, so an in-sheet close is the only way out."
    )

    # And it should be wired to an X / PanelLeftClose / Menu icon.
    has_close_icon = any(
        name in src for name in ("X,", "X ", "X}", "PanelLeftClose")
    )
    assert has_close_icon, (
        "app-sidebar.tsx mobile-close button must use a recognisable close "
        "icon (X from lucide-react or PanelLeftClose)."
    )


def test_mobile_close_button_has_accessible_label() -> None:
    """The in-sheet mobile close button must carry an accessible
    label (sr-only span or aria-label)."""
    src = _read(APP_SIDEBAR)

    # Look for "Close sidebar" or "Close menu" or "Toggle sidebar"
    # in a sr-only span near setOpenMobile(false).
    close_phrases = ["Close sidebar", "Close menu", "Close navigation"]
    has_label = any(phrase in src for phrase in close_phrases)
    assert has_label, (
        "Mobile close button must announce itself to screen readers. "
        f"Expected one of {close_phrases!r} in a sr-only span or aria-label."
    )


# ---------------------------------------------------------------------------
# Force-open effect must respect explicit user dismissal
# ---------------------------------------------------------------------------


def test_force_open_mobile_effect_does_not_loop() -> None:
    """The mobile auto-open effect must not re-fire on every render
    while the user has explicitly dismissed the sheet. The previous
    implementation watched `[isMobile, state, setOpenMobile]` and
    called `setOpenMobile(true)` whenever `state === 'expanded'`,
    which meant closing the sheet (which only changes `openMobile`,
    not `state`) left the effect armed to re-open on the next tick.

    The fix removes the effect entirely (the SidebarProvider's
    `defaultOpen` + mobile sheet's own open/close state already give
    the user the right initial experience) OR guards it so it only
    runs once on the isMobile transition.
    """
    src = _read(APP_SIDEBAR)

    # The simplest robust pin: the auto-open effect, if it still
    # exists, must NOT depend on `state` (which doesn't change when
    # the user closes the sheet). We check that no `useEffect` block
    # both calls setOpenMobile(true) AND has `state` in its
    # dependency array.
    effect_pattern = re.compile(
        r"React\.useEffect\(\s*\(\)\s*=>\s*\{(.*?)\}\s*,\s*\[(.*?)\]\s*\)",
        re.DOTALL,
    )
    for body, deps in effect_pattern.findall(src):
        calls_open_true = re.search(r"setOpenMobile\(\s*true\s*\)", body)
        if calls_open_true:
            assert "state" not in deps, (
                "An auto-open effect that calls setOpenMobile(true) must not "
                "depend on `state` — `state` doesn't change when the user "
                "closes the sheet, so this would re-fire and re-open the "
                f"sheet on every render. Effect deps: [{deps}]"
            )


# ---------------------------------------------------------------------------
# Cross-check: trigger button is reachable at every viewport
# ---------------------------------------------------------------------------


def test_no_viewport_lacks_a_sidebar_dismiss_affordance() -> None:
    """Combine the above into a single end-to-end assertion: at every
    viewport break, the user has at least one visible toggle.

      * 0-767 px (mobile, sheet overlay):
          - Header Menu button (lg:hidden = visible)
          - In-sheet close button (isMobile gated)
      * 768-1023 px (tablet, desktop sidebar):
          - Header Menu button (lg:hidden = visible)
          - In-sidebar PanelLeftClose (!isMobile gated)
      * 1024+ px (desktop):
          - In-sidebar PanelLeftClose (!isMobile gated)
    """
    header_src = _read(HEADER)
    sidebar_src = _read(APP_SIDEBAR)

    # 0-1023: header trigger present and visible
    assert "toggleSidebar" in header_src and "lg:hidden" in header_src

    # 0-767: in-sheet close
    assert re.search(r"setOpenMobile\(\s*false\s*\)", sidebar_src), (
        "Missing in-sheet mobile close button"
    )

    # 768+: in-sidebar PanelLeft toggle
    assert "PanelLeftClose" in sidebar_src or "PanelLeftOpen" in sidebar_src, (
        "Missing in-sidebar desktop toggle"
    )
