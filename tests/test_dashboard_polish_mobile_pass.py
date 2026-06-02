"""Regression-guard pins for the dashboard polish + mobile-responsive pass.

Background. The audit (see `/tmp/dashboard-audit-20260601-222752Z/AUDIT.md`
in the bg-agent transcript, mirrored to the PR body) found 20 cross-
cutting issues across the 7 dashboards. The biggest groups:

  * Tasks dashboard hard-codes 40 lines of `slate-*`, `white/*`,
    `teal-*` palette classes that bypass the shadcn theme tokens.
  * 18 of 20 `<DialogContent>` usages lack the
    `w-[calc(100vw-2rem)]` mobile-width fallback established by
    PRs #54 / #49 / #65 — these clip awkwardly at 375 px.
  * Every data-table page (`tasks`, `agents`, `messages`,
    `memories`) renders `<Table>` at every viewport; rows overflow
    horizontally at 375 px with no card-list alternative.
  * Zero `<Skeleton>` references anywhere — loading falls back to
    "Loading…" text or blank panes.
  * Empty states are duplicated across files with inconsistent
    styling; one is a CC-1 anti-pattern offender. Messages page
    has no empty state at all.
  * Layout chrome: header has no per-page title (mobile users have
    no breadcrumb once the sheet closes), sidebar footer still
    says "Improved Dashboard" (beta-leftover marketing), main
    layout wraps every page render in `animate-fade-in` (busy
    motion combined with shadcn dialog enters).
  * Settings rows don't reflow on mobile, prompt-book has one
    `text-blue-800` palette hardcode.

This file pins the structural fixes so a future refactor can't
silently re-introduce any of them. Tests parse the .tsx files for
required-class / forbidden-class patterns; no dashboard runtime
needed (we keep this lightweight per the test_dashboard_*polish*
pattern set by PR #54).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("agent_mcp/dashboard")
COMPONENTS = ROOT / "components"
DASHBOARDS = COMPONENTS / "dashboard"
LAYOUT = COMPONENTS / "layout"
MODALS = DASHBOARDS / "modals"
SERVER = COMPONENTS / "server"
HOOKS = ROOT / "hooks"


def _read(p: Path) -> str:
    return p.read_text()


# ---------------------------------------------------------------------------
# CC-1 / CC-2 — Tasks dashboard palette migration to theme tokens
# ---------------------------------------------------------------------------

TASKS = DASHBOARDS / "tasks-dashboard.tsx"

# Patterns the audit identified as theme-bypass hardcodes. Each is a
# substring search; if even one appears anywhere in tasks-dashboard.tsx,
# the migration is incomplete.
TASKS_FORBIDDEN_SUBSTRINGS = [
    "bg-slate-",
    "bg-white/",
    "text-white dark:",
    "text-slate-",
    "border-teal-",
    "focus:ring-teal-",
    "focus:border-teal-",
    "shadow-teal-",
    "text-teal-",
    "bg-teal-",
]


def test_tasks_dashboard_uses_theme_tokens_not_raw_palette() -> None:
    """Tasks dashboard must use shadcn semantic tokens (bg-card,
    text-foreground, border-border, text-primary, focus-visible:ring-ring,
    etc.) instead of raw Tailwind palette classes that bypass the theme
    system and duplicate light/dark variants inline.

    The audit counted 40 hits in this single file (the only dashboard
    that does this). Migrating to tokens both fixes the modern-minimal
    aesthetic violation AND makes a future theme tweak a one-place
    change in tailwind.config.ts / globals.css.
    """
    src = _read(TASKS)
    hits: dict[str, int] = {}
    for sub in TASKS_FORBIDDEN_SUBSTRINGS:
        n = src.count(sub)
        if n:
            hits[sub] = n
    assert not hits, (
        "tasks-dashboard.tsx still contains theme-bypass palette "
        "hardcodes (CC-1/CC-2): "
        + ", ".join(f"{k!r}×{v}" for k, v in sorted(hits.items()))
        + ". Migrate to shadcn semantic tokens: bg-card / bg-muted / "
        "border-border / text-foreground / text-muted-foreground / "
        "text-primary / border-primary/20 / focus-visible:ring-ring."
    )


# ---------------------------------------------------------------------------
# CC-14 — every <DialogContent> needs the mobile-width fallback
# ---------------------------------------------------------------------------

DIALOG_CONTENT_FILES = [
    DASHBOARDS / "tasks-dashboard.tsx",
    DASHBOARDS / "agents-dashboard.tsx",
    DASHBOARDS / "memories-dashboard.tsx",
    DASHBOARDS / "messages-dashboard.tsx",
    DASHBOARDS / "prompt-book-dashboard.tsx",
    DASHBOARDS / "task-details-dialog.tsx",
    MODALS / "view-memory-modal.tsx",
    MODALS / "create-memory-modal.tsx",
    MODALS / "edit-memory-modal.tsx",
    MODALS / "delete-memory-modal.tsx",
    MODALS / "create-prompt-modal.tsx",
    DASHBOARDS / "onboarding" / "prompt-book-tutorial.tsx",
    SERVER / "server-management-modal.tsx",
]

# Match `<DialogContent ... className="..."` with the className value
# captured. Multi-line tolerant.
DIALOG_CONTENT_RE = re.compile(
    r'<DialogContent\b[^>]*?\bclassName=\{?"([^"]*)"',
    flags=re.DOTALL,
)


def test_every_dialog_content_has_mobile_width_fallback() -> None:
    """Every <DialogContent> must include `w-[calc(100vw-2rem)]` so it
    fits with a 1-rem-each-side gutter on 375 px viewports. Without the
    fallback, dialogs widen to whatever their `sm:max-w-*` says even on
    sub-sm viewports and clip/overflow horizontally.

    Pattern was established by:
      * PR #54 (tasks View dialog layout polish)
      * PR #49 (tasks-page row-body click View dialog)
      * PR #65 (agent popup MCP-onboarding tabs + polish)

    Sweep applies the same fix to the remaining 18 DialogContent
    usages found by the audit.
    """
    failures: list[str] = []
    for f in DIALOG_CONTENT_FILES:
        assert f.is_file(), f"DialogContent audit target missing: {f}"
        src = _read(f)
        for m in DIALOG_CONTENT_RE.finditer(src):
            classes = m.group(1)
            if "w-[calc(100vw-2rem)]" not in classes:
                # Line number for the failure message.
                line = src[: m.start()].count("\n") + 1
                snippet = classes[:80] + ("…" if len(classes) > 80 else "")
                failures.append(f"{f}:{line}  className={snippet!r}")
    assert not failures, (
        "DialogContent missing mobile-width fallback "
        "`w-[calc(100vw-2rem)]` at "
        + str(len(failures))
        + " sites:\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# CC-7 — mobile card-list sibling for every data-table dashboard
# ---------------------------------------------------------------------------

TABLE_DASHBOARDS = {
    "tasks": DASHBOARDS / "tasks-dashboard.tsx",
    "agents": DASHBOARDS / "agents-dashboard.tsx",
    "messages": DASHBOARDS / "messages-dashboard.tsx",
    "memories": DASHBOARDS / "memories-dashboard.tsx",
}


def test_data_tables_have_mobile_card_list_sibling() -> None:
    """At < sm: viewports the desktop <Table> renders are unusable
    (5-7 columns × 10+ rows horizontally overflow on 375 px). Each
    list dashboard must import a sibling `*-mobile-list` component
    AND render the `hidden sm:block` (table) / `block sm:hidden`
    (mobile) twin guard.
    """
    failures: list[str] = []
    for slug, path in TABLE_DASHBOARDS.items():
        src = _read(path)
        # Look for `*-mobile-list` import (camelCase or kebab-case form).
        mobile_import = re.search(
            rf"from\s+[\"']@/components/dashboard/{slug}-mobile-list[\"']",
            src,
        )
        if not mobile_import:
            failures.append(
                f"{path}: no `import … from '@/components/dashboard/"
                f"{slug}-mobile-list'` found"
            )
            continue
        # Look for the twin-guard idiom: hidden sm:block AND sm:hidden.
        if "hidden sm:block" not in src:
            failures.append(
                f"{path}: no `hidden sm:block` class (table-only guard) found"
            )
        if "sm:hidden" not in src:
            failures.append(
                f"{path}: no `sm:hidden` class (mobile-only guard) found"
            )
    assert not failures, (
        "Mobile card-list conversion incomplete (CC-7):\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# CC-3 — Skeleton loading
# ---------------------------------------------------------------------------


def test_list_dashboards_use_skeleton_loading() -> None:
    """Each list dashboard (tasks, agents, messages, memories,
    prompt-book) must import `Skeleton` from `@/components/ui/skeleton`
    — either directly or transitively via a per-page loading
    sub-component. Today every page falls back to "Loading…" text or
    blank, which is sloppy. The shadcn primitive ships in the repo
    but is unused.
    """
    targets = {
        "tasks": DASHBOARDS / "tasks-dashboard.tsx",
        "agents": DASHBOARDS / "agents-dashboard.tsx",
        "messages": DASHBOARDS / "messages-dashboard.tsx",
        "memories": DASHBOARDS / "memories-dashboard.tsx",
        "prompt-book": DASHBOARDS / "prompt-book-dashboard.tsx",
    }
    failures: list[str] = []
    for slug, path in targets.items():
        src = _read(path)
        direct = re.search(
            r"from\s+[\"']@/components/ui/skeleton[\"']", src
        )
        # OR: imports a per-page loading sub-component that uses Skeleton.
        loading_sub_import = re.search(
            rf"from\s+[\"']@/components/dashboard/{slug}-loading[\"']",
            src,
        )
        loading_sub_uses_skeleton = False
        if loading_sub_import:
            loading_path = DASHBOARDS / f"{slug}-loading.tsx"
            if loading_path.is_file():
                loading_src = _read(loading_path)
                loading_sub_uses_skeleton = bool(re.search(
                    r"from\s+[\"']@/components/ui/skeleton[\"']",
                    loading_src,
                ))
        if not (direct or loading_sub_uses_skeleton):
            failures.append(
                f"{path}: neither direct Skeleton import nor a "
                f"{slug}-loading sub-component using Skeleton"
            )
    assert not failures, (
        "Skeleton loading missing (CC-3):\n  " + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# CC-6 — shared <EmptyState> primitive used everywhere
# ---------------------------------------------------------------------------

EMPTY_STATE_PRIMITIVE = DASHBOARDS / "shared" / "empty-state.tsx"


def test_empty_state_primitive_exists() -> None:
    assert EMPTY_STATE_PRIMITIVE.is_file(), (
        f"expected shared EmptyState primitive at {EMPTY_STATE_PRIMITIVE}. "
        "Pulls the duplicated per-page empty-state markup into one "
        "place + lets us drop the tasks-dashboard slate/teal "
        "anti-pattern as a side-effect (CC-1 overlap)."
    )


def test_list_dashboards_import_empty_state() -> None:
    """Each list dashboard + messages dashboard imports the shared
    EmptyState primitive (CC-6, CC-20)."""
    targets = [
        DASHBOARDS / "tasks-dashboard.tsx",
        DASHBOARDS / "agents-dashboard.tsx",
        DASHBOARDS / "memories-dashboard.tsx",
        DASHBOARDS / "messages-dashboard.tsx",
        DASHBOARDS / "prompt-book-dashboard.tsx",
    ]
    failures: list[str] = []
    for path in targets:
        src = _read(path)
        if not re.search(
            r"from\s+[\"']@/components/dashboard/shared/empty-state[\"']",
            src,
        ):
            failures.append(str(path))
    assert not failures, (
        "EmptyState primitive not imported (CC-6/CC-20):\n  "
        + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# CC-9 — header per-page title
# ---------------------------------------------------------------------------

HEADER = LAYOUT / "header.tsx"


def test_header_renders_per_page_title() -> None:
    """The header must surface the current page name so mobile users
    have a breadcrumb once the sidebar sheet closes. Look for any
    reference to `currentView` (the dashboard store hook that knows
    which page is active).
    """
    src = _read(HEADER)
    assert "currentView" in src, (
        f"{HEADER}: no `currentView` reference. Header should derive a "
        "page title from the dashboard store so mobile users have a "
        "breadcrumb when the sidebar is closed."
    )


# ---------------------------------------------------------------------------
# CC-15 — drop animate-fade-in from main layout
# ---------------------------------------------------------------------------

MAIN_LAYOUT = LAYOUT / "main-layout.tsx"


def test_main_layout_drops_animate_fade_in() -> None:
    """`animate-fade-in` on every page render combined with shadcn
    dialog enters and sidebar tooltip animations reads as busy
    motion. Modern-minimal calls for component-level transitions
    (150 ms ease on hover/focus) — let those own motion. Drop the
    layout-level fade.
    """
    src = _read(MAIN_LAYOUT)
    assert "animate-fade-in" not in src, (
        f"{MAIN_LAYOUT}: `animate-fade-in` should be removed from the "
        "main-content wrapper. Layout-level page-fade animation is "
        "noisy combined with the rest of the UI's motion."
    )


# ---------------------------------------------------------------------------
# CC-17 — drop hard-coded text-blue-800 in prompt-book
# ---------------------------------------------------------------------------

PROMPT_BOOK = DASHBOARDS / "prompt-book-dashboard.tsx"


def test_prompt_book_drops_hardcoded_blue() -> None:
    src = _read(PROMPT_BOOK)
    assert "text-blue-800" not in src, (
        f"{PROMPT_BOOK}: `text-blue-800` is a theme-bypass hardcode. "
        "Switch to `text-foreground` on a `bg-muted/50` container — "
        "matches the modern-minimal monochrome palette + works under "
        "both light and dark themes."
    )


# ---------------------------------------------------------------------------
# CC-10 — sidebar footer no longer says "Improved Dashboard"
# ---------------------------------------------------------------------------

APP_SIDEBAR = LAYOUT / "app-sidebar.tsx"


def test_sidebar_drops_improved_dashboard_tagline() -> None:
    src = _read(APP_SIDEBAR)
    assert "Improved Dashboard" not in src, (
        f"{APP_SIDEBAR}: the `Improved Dashboard` tagline reads as "
        "leftover beta-marketing copy. Drop it — show just the "
        "product name + version."
    )


# ---------------------------------------------------------------------------
# CC-18 — settings policy rows reflow on mobile
# ---------------------------------------------------------------------------

SETTINGS = DASHBOARDS / "settings-dashboard.tsx"


def test_settings_policy_rows_reflow_on_mobile() -> None:
    """Settings policy rows pair a long description with a Switch on
    the right via `flex items-start justify-between`. At 375 px the
    description squashes the Switch. Apply `flex-col sm:flex-row`
    so the Switch drops below the description on mobile.
    """
    src = _read(SETTINGS)
    assert "flex-col sm:flex-row" in src, (
        f"{SETTINGS}: no `flex-col sm:flex-row` reflow class found. "
        "Policy / retention rows should stack vertically at < sm: and "
        "lay out horizontally at >= sm:."
    )


# ---------------------------------------------------------------------------
# CC-20 — messages page imports EmptyState (covered by
# test_list_dashboards_import_empty_state above)
# ---------------------------------------------------------------------------
# Explicit dedicated test in case the broader assertion changes:


def test_messages_dashboard_imports_empty_state() -> None:
    src = _read(DASHBOARDS / "messages-dashboard.tsx")
    assert re.search(
        r"from\s+[\"']@/components/dashboard/shared/empty-state[\"']", src
    ), (
        "messages-dashboard.tsx must import the shared EmptyState "
        "primitive (CC-20). Today the page renders `0 messages` in "
        "the CardHeader with no empty body — users get a confusing "
        "blank table region. Render <EmptyState> when "
        "filteredMessages.length === 0."
    )


# ---------------------------------------------------------------------------
# CC-25 — mobile sidebar Sheet auto-closes on nav-item click
# ---------------------------------------------------------------------------

NAVIGATION = LAYOUT / "navigation.tsx"


def test_navigation_closes_mobile_sheet_on_nav_click() -> None:
    """When the mobile Sheet sidebar is open, clicking a nav item must
    auto-close the Sheet (iOS-style navigation UX). Currently the user
    has to manually dismiss the Sheet after picking the page.

    Wire `setOpenMobile(false)` (from `useSidebar()` in the shadcn
    Sidebar primitive) into the NavButton onClick handler.
    """
    src = _read(NAVIGATION)
    assert "setOpenMobile" in src, (
        f"{NAVIGATION}: no `setOpenMobile` reference. Navigation must "
        "close the mobile Sheet after a nav-item click; otherwise the "
        "user is stranded with the sheet still over their content "
        "after they pick a page (verified at 375 px in the audit "
        "screenshots)."
    )


# ---------------------------------------------------------------------------
# CC-24 — Prompt Book Tabs deduplication (no two TabsTrigger with same value)
# ---------------------------------------------------------------------------


def test_prompt_book_tabs_do_not_truncate_to_first_word() -> None:
    """The Prompt Book Tabs render `{category.name.split(' ')[0]}`
    which truncates "Agent Initialization" and "Agent Coordination"
    both to "Agent" — verified in the 375 px audit screenshot
    where two adjacent tabs both read "Agent".

    Fix: render the full `category.name` (and use overflow-x-auto
    on the TabsList so the tabs scroll horizontally on mobile).
    """
    src = _read(PROMPT_BOOK)
    # Locate the TabsTrigger block and inspect its inner content.
    # Look for `category.name.split(' ')[0]` exactly — that's the bug.
    assert "category.name.split(' ')[0]" not in src, (
        f"{PROMPT_BOOK}: TabsTrigger renders `category.name."
        "split(' ')[0]` — splits multi-word category names ("
        "'Agent Initialization', 'Agent Coordination') to a "
        "single ambiguous first word ('Agent', 'Agent'). Render the "
        "full `category.name` and let the TabsList overflow-x-auto "
        "handle narrow viewports."
    )


# ---------------------------------------------------------------------------
# CC-23 — Prompt Book header action buttons stack on mobile
# ---------------------------------------------------------------------------


def test_prompt_book_header_actions_reflow_on_mobile() -> None:
    """The Prompt Book page header action group (count badges +
    Create Prompt + Help buttons) is wrapped in a `flex items-center
    gap-2` container with NO `flex-wrap`. At 375 px the inner row
    overflows the right edge (visible in the audit screenshot —
    "Create Prompt" cut to "Compo" then "Help" cut entirely).

    The outer header IS already `flex-col sm:flex-row` (line 401),
    so the action group drops to its own row on mobile — but within
    that row, the 4 badges + 2 buttons still need `flex-wrap` to
    avoid horizontal overflow.

    Pin: the action group container near "Create Prompt" must have
    `flex-wrap` somewhere in its className.
    """
    src = _read(PROMPT_BOOK)
    # Find the Create Prompt button anchor.
    cp_idx = src.find("Create Prompt")
    assert cp_idx >= 0, "Create Prompt button text not found"
    # Walk backwards to find the wrapping <div className="...">.
    region_start = src.rfind('<div className="', 0, cp_idx)
    assert region_start >= 0, "couldn't locate action-group container"
    region_end = src.find('">', region_start) + 2
    container_tag = src[region_start:region_end]
    assert "flex-wrap" in container_tag, (
        f"{PROMPT_BOOK}: the action-group div containing the "
        "Create Prompt button must include `flex-wrap` so the 4 "
        "badges + 2 buttons can break to multiple rows on narrow "
        "viewports (audit found horizontal overflow at 375 px). "
        f"Current container tag:\n  {container_tag}"
    )


# ---------------------------------------------------------------------------
# CC-21 — Tasks H1 visible in light mode (root cause is the same
# `text-white dark:text-white text-slate-900` anti-pattern covered
# by CC-1 forbidden substrings — Tailwind v4 emits utilities in
# alphabetical not source order, so `text-white` ships AFTER
# `text-slate-900` in the stylesheet → always-on white wins and
# the H1 vanishes on light backgrounds). Explicit regression test:
# ---------------------------------------------------------------------------


def test_tasks_h1_uses_text_foreground() -> None:
    """The Tasks page H1 today reads `text-fluid-2xl font-bold
    text-white dark:text-white text-slate-900` — Tailwind v4 emits
    `text-white` AFTER `text-slate-900` in the generated stylesheet
    (utilities are ordered, not source-order), so the always-on
    `text-white` wins over the always-on `text-slate-900` and the
    H1 disappears on light-mode white backgrounds. Switch to
    `text-foreground` (semantic token).
    """
    src = _read(TASKS)
    title_idx = src.find("Task Operations")
    assert title_idx >= 0, "Tasks H1 'Task Operations' text not found"
    region_start = src.rfind("<h1", 0, title_idx)
    region_end = src.find(">", region_start) + 1
    assert region_start >= 0 and region_end > region_start, (
        "couldn't locate the H1 JSX tag wrapping 'Task Operations'"
    )
    h1_tag = src[region_start:region_end]
    assert "text-foreground" in h1_tag, (
        f"{TASKS}: Tasks H1 must use `text-foreground` (semantic "
        "token), not the broken `text-white dark:text-white "
        "text-slate-900` triplet that resolves to invisible white "
        "text in light mode. Current H1 tag:\n  " + h1_tag[:200]
    )
