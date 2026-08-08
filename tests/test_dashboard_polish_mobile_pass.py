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

Contract update — delegation to the shared scaffold
---------------------------------------------------

The audit was originally written against an architecture where every
list page re-implemented its own header, skeleton, empty state and
mobile card-list. The shared foundation (`<DataTablePage>` +
`<ResponsiveDataTable>`) now owns those concerns, so several of these
guarantees are **satisfied directly OR by delegation**:

  * CC-3 (Skeleton), CC-6/CC-20 (EmptyState) and CC-7 (mobile
    card-list) pass if the page carries the marker itself *or* renders
    through `<DataTablePage>`.
  * The scaffold is then audited DIRECTLY for each guarantee it
    absorbs (`test_data_table_page_provides_skeleton_loading`,
    `test_data_table_page_provides_empty_state`,
    `test_responsive_data_table_renders_mobile_twin_guard`,
    `test_data_table_page_renders_responsive_table`), so delegation is
    an equivalence rather than an exemption.
  * `test_delegation_detection_is_not_an_escape_hatch` pins the
    negative cases, so a page that neither carries the marker nor
    genuinely delegates still fails.

Two file manifests that used to be hardcoded (the `<DialogContent>`
set and the CC-7 table-dashboard set) are now derived from the tree.
A hardcoded list breaks when a file is legitimately deleted — that is
what happened when `delete-memory-modal.tsx` was subsumed by the
unified `<DeleteConfirmModal>` — and silently skips files nobody
remembers to add.
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
# Shared list-page scaffold — the delegation target
# ---------------------------------------------------------------------------
#
# The CC-3 / CC-6 / CC-7 guarantees below were originally written when
# EVERY list page re-implemented its own skeleton, empty state and
# mobile card-list. The shared foundation (`<DataTablePage>` +
# `<ResponsiveDataTable>`) now owns those concerns for any page that
# delegates to it, so a per-page grep alone no longer describes the
# architecture — a migrated page provably still HAS the behaviour, it
# just doesn't spell it out inline.
#
# The contract these tests enforce is therefore:
#
#     a page satisfies CC-3 / CC-6 / CC-7 if it contains the marker
#     ITSELF **or** it renders through <DataTablePage>
#
# and — so this is a real equivalence rather than an escape hatch — the
# scaffold itself is audited directly for each guarantee it absorbs
# (see the `test_data_table_page_*` / `test_responsive_data_table_*`
# tests). Net coverage is therefore stronger than the pre-delegation
# audit: the shared components are now pinned too, and a page that
# neither carries the marker nor delegates still FAILS (pinned by
# `test_delegation_detection_is_not_an_escape_hatch`).

DATA_TABLE_PAGE = DASHBOARDS / "shared" / "data-table-page.tsx"
RESPONSIVE_DATA_TABLE = DASHBOARDS / "shared" / "responsive-data-table.tsx"
DASHBOARD_HEADER = DASHBOARDS / "shared" / "dashboard-header.tsx"

_SCAFFOLD_IMPORT_RE = re.compile(
    r"from\s+[\"']@/components/dashboard/shared/data-table-page[\"']"
)


def _delegates_to_scaffold(src: str) -> bool:
    """True if this page renders its list through `<DataTablePage>`.

    Requires BOTH the import and an actual `<DataTablePage` render, so
    a stray type-only import can't be used to opt out of the audit.
    """
    return bool(_SCAFFOLD_IMPORT_RE.search(src)) and "<DataTablePage" in src


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

# Audit surface for CC-14, enumerated DYNAMICALLY.
#
# This used to be a hardcoded list of 13 paths. That list was a drift
# source in both directions: it silently skipped app dialogs nobody
# remembered to add (it was missing 9 files carrying `<DialogContent>`,
# one of which had a real un-audited violation), and it hard-FAILED the
# whole suite when a listed file was legitimately deleted — which is
# exactly what happened when `delete-memory-modal.tsx` was subsumed by
# the unified `<DeleteConfirmModal>`. A file list that breaks on a
# legitimate deletion is testing the manifest, not the code.
#
# Globbing the component tree instead means "every dialog we ship" is
# the audit surface, automatically, forever.
#
# `components/ui/` is excluded: those are vendored shadcn primitives
# (e.g. `command.tsx`'s CommandDialog), not app-authored dialogs — the
# app-level wrappers around them ARE covered.
DIALOG_CONTENT_ROOTS = [DASHBOARDS, SERVER]


def _dialog_content_files() -> list[Path]:
    found: list[Path] = []
    for root in DIALOG_CONTENT_ROOTS:
        for f in sorted(root.rglob("*.tsx")):
            if f.name.endswith(".test.tsx"):
                continue
            if "<DialogContent" in _read(f):
                found.append(f)
    return found

# Match `<DialogContent ... className="..."` with the className value
# captured. Multi-line tolerant.
DIALOG_CONTENT_RE = re.compile(
    r'<DialogContent\b[^>]*?\bclassName=\{?"([^"]*)"',
    flags=re.DOTALL,
)

# Every `<DialogContent` opening tag, className or not.
#
# The className-anchored pattern above cannot see a dialog written as a
# bare `<DialogContent>` — it simply does not match, so the site is
# skipped SILENTLY. That is not a theoretical hole: the schedules page
# shipped two of them, and its delete confirm sat outside this audit
# (while clipping on a phone, which is exactly what the audit exists to
# catch) for as long as it existed. A classless DialogContent has no
# mobile-width fallback BY CONSTRUCTION, so it is always a violation —
# counting it as one is what makes the audit's surface equal to its
# glob.
DIALOG_CONTENT_ANY_RE = re.compile(r"<DialogContent\b")

# Comments legitimately WRITE `<DialogContent>` when explaining this
# very audit, so both passes read comment-stripped source. Otherwise a
# doc-comment counts as a dialog and the classless check reports files
# that are actually fine.
_JSX_COMMENT_RE = re.compile(r"\{/\*[\s\S]*?\*/\}")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", flags=re.MULTILINE)


def _code_only(src: str) -> str:
    src = _JSX_COMMENT_RE.sub("", src)
    src = _BLOCK_COMMENT_RE.sub("", src)
    return _LINE_COMMENT_RE.sub("", src)


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

    The file set is globbed (see `_dialog_content_files`), so a dialog
    added tomorrow is audited automatically and a dialog legitimately
    deleted doesn't red the suite.
    """
    files = _dialog_content_files()
    assert files, (
        "no <DialogContent> sites found under "
        f"{[str(r) for r in DIALOG_CONTENT_ROOTS]} — the glob is broken, "
        "which would silently disable this audit"
    )
    failures: list[str] = []
    for f in files:
        src = _code_only(_read(f))
        for m in DIALOG_CONTENT_RE.finditer(src):
            classes = m.group(1)
            if "w-[calc(100vw-2rem)]" not in classes:
                # Line number for the failure message.
                line = src[: m.start()].count("\n") + 1
                snippet = classes[:80] + ("…" if len(classes) > 80 else "")
                failures.append(f"{f}:{line}  className={snippet!r}")
        # Classless tags: every `<DialogContent` must have been reached
        # by the className-anchored pass above. Any surplus is a dialog
        # this audit could not see.
        seen = len(DIALOG_CONTENT_RE.findall(src))
        total = len(DIALOG_CONTENT_ANY_RE.findall(src))
        if total > seen:
            failures.append(
                f"{f}  {total - seen} <DialogContent> with NO className "
                "(invisible to this audit, and with no mobile-width "
                "fallback by construction)"
            )
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

def _table_dashboards() -> dict[str, Path]:
    """Derive the CC-7 audit set instead of hardcoding paths.

    A page is in scope if it either ships a `*-mobile-list.tsx` sibling
    (the pre-scaffold idiom) or renders through `<DataTablePage>` (the
    scaffold idiom). Both halves are computed from the tree, so a new
    list page or a new migration joins the audit with no edit here, and
    deleting a file can't break the manifest.
    """
    targets: dict[str, Path] = {}
    for mobile in sorted(DASHBOARDS.glob("*-mobile-list.tsx")):
        slug = mobile.name[: -len("-mobile-list.tsx")]
        page = DASHBOARDS / f"{slug}-dashboard.tsx"
        if page.is_file():
            targets[slug] = page
    for page in sorted(DASHBOARDS.glob("*-dashboard.tsx")):
        if _delegates_to_scaffold(_read(page)):
            targets[page.name[: -len("-dashboard.tsx")]] = page
    return targets


def test_data_tables_have_mobile_card_list_sibling() -> None:
    """At < sm: viewports the desktop <Table> renders are unusable
    (5-7 columns × 10+ rows horizontally overflow on 375 px). Each
    list dashboard must therefore offer a mobile card alternative —
    satisfied EITHER directly (import a `*-mobile-list` sibling and
    render the `hidden sm:block` / `block sm:hidden` twin guard) OR by
    delegating to `<DataTablePage>`, which renders the twin guard
    inside `<ResponsiveDataTable>` from one column spec.

    The scaffold's own guarantee is pinned by
    `test_responsive_data_table_renders_mobile_twin_guard`, so the
    delegation branch is an equivalence, not an exemption.
    """
    targets = _table_dashboards()
    assert targets, "CC-7 audit set derived empty — the derivation is broken"
    failures: list[str] = []
    for slug, path in targets.items():
        src = _read(path)
        # Branch 1 — the page delegates the whole table shell.
        if _delegates_to_scaffold(src):
            continue
        # Branch 2 — the page renders its own table + mobile twin.
        mobile_import = re.search(
            rf"from\s+[\"']@/components/dashboard/{slug}-mobile-list[\"']",
            src,
        )
        if not mobile_import:
            failures.append(
                f"{path}: neither renders via <DataTablePage> nor imports "
                f"'@/components/dashboard/{slug}-mobile-list'"
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


def test_responsive_data_table_renders_mobile_twin_guard() -> None:
    """The scaffold half of CC-7: `<ResponsiveDataTable>` must itself
    render the desktop/mobile twin guard, because every page that
    delegates inherits its mobile behaviour from exactly this file.
    """
    assert RESPONSIVE_DATA_TABLE.is_file(), (
        f"expected the shared responsive table at {RESPONSIVE_DATA_TABLE}"
    )
    src = _read(RESPONSIVE_DATA_TABLE)
    missing = [c for c in ("hidden sm:block", "sm:hidden") if c not in src]
    assert not missing, (
        f"{RESPONSIVE_DATA_TABLE}: missing twin-guard class(es) "
        f"{missing}. Every <DataTablePage> page inherits its mobile "
        "card-list from this component (CC-7)."
    )


def test_data_table_page_renders_responsive_table() -> None:
    """`<DataTablePage>` must actually route its rows through
    `<ResponsiveDataTable>` — otherwise the CC-7 delegation branch
    above would be vacuous.
    """
    assert DATA_TABLE_PAGE.is_file(), (
        f"expected the shared list-page scaffold at {DATA_TABLE_PAGE}"
    )
    src = _read(DATA_TABLE_PAGE)
    assert "<ResponsiveDataTable" in src, (
        f"{DATA_TABLE_PAGE}: does not render <ResponsiveDataTable>. "
        "Pages delegating to the scaffold would then have no mobile "
        "card-list at all (CC-7)."
    )


# ---------------------------------------------------------------------------
# CC-3 — Skeleton loading
# ---------------------------------------------------------------------------


def test_list_dashboards_use_skeleton_loading() -> None:
    """Each list dashboard (tasks, agents, messages, memories,
    prompt-book) must render a `Skeleton` loading state — satisfied by
    a direct `@/components/ui/skeleton` import, a per-page
    `*-loading` sub-component that imports it, OR delegation to
    `<DataTablePage>` (which owns the stats+rows skeleton for every
    page that renders through it — pinned by
    `test_data_table_page_provides_skeleton_loading`).

    Before any of these existed every page fell back to "Loading…"
    text or a blank pane, which is sloppy; the shadcn primitive
    shipped in the repo but was unused.
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
        if not (direct or loading_sub_uses_skeleton or _delegates_to_scaffold(src)):
            failures.append(
                f"{path}: no direct Skeleton import, no {slug}-loading "
                "sub-component using Skeleton, and no <DataTablePage> "
                "delegation"
            )
    assert not failures, (
        "Skeleton loading missing (CC-3):\n  " + "\n  ".join(failures)
    )


def test_data_table_page_provides_skeleton_loading() -> None:
    """The scaffold half of CC-3: every page delegating to
    `<DataTablePage>` inherits its loading state from this file, so
    the file must actually import and render the Skeleton primitive.
    """
    src = _read(DATA_TABLE_PAGE)
    assert re.search(r"from\s+[\"']@/components/ui/skeleton[\"']", src), (
        f"{DATA_TABLE_PAGE}: does not import the Skeleton primitive. "
        "Pages delegating to the scaffold would then have no skeleton "
        "loading state (CC-3)."
    )
    assert "<Skeleton" in src, (
        f"{DATA_TABLE_PAGE}: imports Skeleton but never renders it (CC-3)."
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


EMPTY_STATE_IMPORT_RE = re.compile(
    r"from\s+[\"']@/components/dashboard/shared/empty-state[\"']"
)


def test_list_dashboards_import_empty_state() -> None:
    """Each list dashboard + messages dashboard renders the shared
    EmptyState primitive (CC-6, CC-20) — satisfied either by importing
    it directly or by delegating to `<DataTablePage>`, which renders
    `<EmptyState>` for its `empty` prop (pinned by
    `test_data_table_page_provides_empty_state`).
    """
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
        if not (
            EMPTY_STATE_IMPORT_RE.search(src) or _delegates_to_scaffold(src)
        ):
            failures.append(str(path))
    assert not failures, (
        "EmptyState primitive not imported (CC-6/CC-20):\n  "
        + "\n  ".join(failures)
    )


def test_delegation_detection_is_not_an_escape_hatch() -> None:
    """Guard the guard.

    The CC-3 / CC-6 / CC-7 tests accept "renders via <DataTablePage>"
    in place of an inline marker. That is only sound while
    `_delegates_to_scaffold` stays strict, so pin its negative cases:
    a page with no scaffold reference, and a page that imports the
    scaffold type but never renders it, must BOTH be treated as
    non-delegating and therefore still be required to carry their own
    markers.
    """
    assert not _delegates_to_scaffold(
        "export function X(){ return <div>plain page</div> }"
    ), "a page with no scaffold reference must not count as delegating"

    import_only = (
        "import type { DataTablePageProps } from "
        "'@/components/dashboard/shared/data-table-page'\n"
        "export function X(){ return <div>hand-rolled</div> }"
    )
    assert not _delegates_to_scaffold(import_only), (
        "importing the scaffold without rendering <DataTablePage> must "
        "not satisfy the audit — otherwise a page could opt out of "
        "CC-3/CC-6/CC-7 with a single unused import"
    )

    render_only = "export function X(){ return <DataTablePage /> }"
    assert not _delegates_to_scaffold(render_only), (
        "a <DataTablePage> render with no matching import must not "
        "count as delegating"
    )

    real = (
        "import { DataTablePage } from "
        "'@/components/dashboard/shared/data-table-page'\n"
        "export function X(){ return <DataTablePage rows={[]} /> }"
    )
    assert _delegates_to_scaffold(real), (
        "a genuine import + render must be recognised as delegating"
    )

    # And the audit must still be watching at least one page that does
    # NOT delegate — otherwise every assertion above is vacuous and the
    # per-page markers would go unchecked repo-wide.
    non_delegating = [
        p
        for p in sorted(DASHBOARDS.glob("*-dashboard.tsx"))
        if not _delegates_to_scaffold(_read(p))
    ]
    assert non_delegating, (
        "every dashboard now delegates — re-point these audits at the "
        "shared components directly instead of leaving them vacuous"
    )


def test_data_table_page_provides_empty_state() -> None:
    """The scaffold half of CC-6/CC-20: pages delegating to
    `<DataTablePage>` inherit their empty state from this file.
    """
    src = _read(DATA_TABLE_PAGE)
    assert EMPTY_STATE_IMPORT_RE.search(src), (
        f"{DATA_TABLE_PAGE}: does not import the shared EmptyState "
        "primitive. Pages delegating to the scaffold would then have no "
        "empty state (CC-6/CC-20)."
    )
    assert "<EmptyState" in src, (
        f"{DATA_TABLE_PAGE}: imports EmptyState but never renders it "
        "(CC-6/CC-20)."
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


def _h1_uses_text_foreground(src: str, title: str) -> tuple[bool, str]:
    """Locate the `<h1>` wrapping `title` and report whether it carries
    `text-foreground`. Returns (ok, detail) — detail is the offending
    tag (or a not-found note) for the failure message.
    """
    title_idx = src.find(title)
    if title_idx < 0:
        return False, f"H1 text {title!r} not found"
    region_start = src.rfind("<h1", 0, title_idx)
    if region_start < 0:
        return False, f"no <h1> tag precedes {title!r}"
    region_end = src.find(">", region_start) + 1
    if region_end <= region_start:
        return False, f"unterminated <h1> tag before {title!r}"
    h1_tag = src[region_start:region_end]
    return "text-foreground" in h1_tag, h1_tag[:200]


def test_tasks_h1_uses_text_foreground() -> None:
    """The Tasks page H1 once read `text-fluid-2xl font-bold
    text-white dark:text-white text-slate-900` — Tailwind v4 emits
    `text-white` AFTER `text-slate-900` in the generated stylesheet
    (utilities are ordered, not source-order), so the always-on
    `text-white` wins over the always-on `text-slate-900` and the
    H1 disappears on light-mode white backgrounds. It must use
    `text-foreground` (semantic token).

    Delegation-aware, same equivalence as CC-3/CC-6/CC-7: the page
    satisfies this either by rendering its own `<h1>` or by handing the
    title to `<DataTablePage header={{title: ...}}>`, whose
    `<DashboardHeader>` renders the `<h1>`. The delegating branch is
    NOT an exemption — it additionally requires that the page really
    passes "Task Operations" as the header title, and the scaffold half
    is pinned directly by
    `test_dashboard_header_h1_uses_text_foreground`.
    """
    src = _read(TASKS)
    if _delegates_to_scaffold(src):
        assert re.search(r"title:\s*['\"]Task Operations['\"]", src), (
            f"{TASKS}: delegates to <DataTablePage> but does not pass "
            "`title: 'Task Operations'` in its `header` prop — the page "
            "would then have no H1 at all (CC-21)."
        )
        return
    ok, detail = _h1_uses_text_foreground(src, "Task Operations")
    assert ok, (
        f"{TASKS}: Tasks H1 must use `text-foreground` (semantic "
        "token), not the broken `text-white dark:text-white "
        "text-slate-900` triplet that resolves to invisible white "
        "text in light mode. Current H1 tag:\n  " + detail
    )


def test_dashboard_header_h1_uses_text_foreground() -> None:
    """The scaffold half of CC-21: every page that hands its title to
    `<DashboardHeader>` (directly or via `<DataTablePage>`) inherits
    its H1 styling from exactly this file, so the shared header must
    itself use the semantic token.
    """
    src = _read(DASHBOARD_HEADER)
    ok, detail = _h1_uses_text_foreground(src, "{title}")
    assert ok, (
        f"{DASHBOARD_HEADER}: the shared header's H1 must use "
        "`text-foreground`; every delegating page's title is rendered "
        "here. Current H1 tag:\n  " + detail
    )
