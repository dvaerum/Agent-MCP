/**
 * Regression-guard pins for the dashboard polish + mobile-responsive pass.
 *
 * Background. The audit (see `/tmp/dashboard-audit-20260601-222752Z/AUDIT.md`
 * in the bg-agent transcript, mirrored to the PR body) found 20 cross-
 * cutting issues across the 7 dashboards. The biggest groups:
 *
 *   * Tasks dashboard hard-codes 40 lines of `slate-*`, `white/*`,
 *     `teal-*` palette classes that bypass the shadcn theme tokens.
 *   * 18 of 20 `<DialogContent>` usages lack the
 *     `w-[calc(100vw-2rem)]` mobile-width fallback established by
 *     PRs #54 / #49 / #65 — these clip awkwardly at 375 px.
 *   * Every data-table page (`tasks`, `agents`, `messages`,
 *     `memories`) renders `<Table>` at every viewport; rows overflow
 *     horizontally at 375 px with no card-list alternative.
 *   * Zero `<Skeleton>` references anywhere — loading falls back to
 *     "Loading…" text or blank panes.
 *   * Empty states are duplicated across files with inconsistent
 *     styling; one is a CC-1 anti-pattern offender. Messages page
 *     has no empty state at all.
 *   * Layout chrome: header has no per-page title (mobile users have
 *     no breadcrumb once the sheet closes), sidebar footer still
 *     says "Improved Dashboard" (beta-leftover marketing), main
 *     layout wraps every page render in `animate-fade-in` (busy
 *     motion combined with shadcn dialog enters).
 *   * Settings rows don't reflow on mobile, prompt-book has one
 *     `text-blue-800` palette hardcode.
 *
 * This file pins the structural fixes so a future refactor can't
 * silently re-introduce any of them. Tests parse the .tsx files for
 * required-class / forbidden-class patterns; no dashboard runtime
 * needed (we keep this lightweight per the house `*-polish*` /
 * `ux-polish.test.ts` test convention).
 *
 * Contract update — delegation to the shared scaffold
 * ---------------------------------------------------
 *
 * The audit was originally written against an architecture where every
 * list page re-implemented its own header, skeleton, empty state and
 * mobile card-list. The shared foundation (`<DataTablePage>` +
 * `<ResponsiveDataTable>`) now owns those concerns, so several of these
 * guarantees are **satisfied directly OR by delegation**:
 *
 *   * CC-3 (Skeleton), CC-6/CC-20 (EmptyState) and CC-7 (mobile
 *     card-list) pass if the page carries the marker itself *or* renders
 *     through `<DataTablePage>`.
 *   * The scaffold is then audited DIRECTLY for each guarantee it
 *     absorbs (`data-table-page-provides-skeleton-loading`,
 *     `data-table-page-provides-empty-state`,
 *     `responsive-data-table-renders-mobile-twin-guard`,
 *     `data-table-page-renders-responsive-table`), so delegation is
 *     an equivalence rather than an exemption.
 *   * `delegation-detection-is-not-an-escape-hatch` pins the
 *     negative cases, so a page that neither carries the marker nor
 *     genuinely delegates still fails.
 *
 * Two file manifests that used to be hardcoded (the `<DialogContent>`
 * set and the CC-7 table-dashboard set) are now derived from the tree.
 * A hardcoded list breaks when a file is legitimately deleted — that is
 * what happened when `delete-memory-modal.tsx` was subsumed by the
 * unified `<DeleteConfirmModal>` — and silently skips files nobody
 * remembers to add.
 */

import { describe, expect, it } from "vitest"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { resolve, join, extname } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const COMPONENTS = resolve(DASHBOARD_ROOT, "components")
const DASHBOARDS = resolve(COMPONENTS, "dashboard")
const LAYOUT_DIR = resolve(COMPONENTS, "layout")
const SERVER_DIR = resolve(COMPONENTS, "server")

const read = (p: string) => readFileSync(p, "utf8")

// Recursively walk a directory, returning absolute paths of files whose
// name matches `pred`. Mirrors Python's `Path.rglob("*.tsx")`.
function rglob(root: string, ext: string): string[] {
  const out: string[] = []
  function walk(dir: string) {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry)
      const st = statSync(full)
      if (st.isDirectory()) {
        walk(full)
      } else if (st.isFile() && extname(full) === ext) {
        out.push(full)
      }
    }
  }
  walk(root)
  return out
}

function glob(root: string, ext: string): string[] {
  return readdirSync(root)
    .filter((f) => extname(f) === ext)
    .sort()
    .map((f) => join(root, f))
}

// ---------------------------------------------------------------------------
// Shared list-page scaffold — the delegation target
// ---------------------------------------------------------------------------
//
// The CC-3 / CC-6 / CC-7 guarantees below were originally written when
// EVERY list page re-implemented its own skeleton, empty state and
// mobile card-list. The shared foundation (`<DataTablePage>` +
// `<ResponsiveDataTable>`) now owns those concerns for any page that
// delegates to it, so a per-page grep alone no longer describes the
// architecture — a migrated page provably still HAS the behaviour, it
// just doesn't spell it out inline.
//
// The contract these tests enforce is therefore:
//
//     a page satisfies CC-3 / CC-6 / CC-7 if it contains the marker
//     ITSELF **or** it renders through <DataTablePage>
//
// and — so this is a real equivalence rather than an escape hatch — the
// scaffold itself is audited directly for each guarantee it absorbs
// (see the `data-table-page-*` / `responsive-data-table-*` tests). Net
// coverage is therefore stronger than the pre-delegation audit: the
// shared components are now pinned too, and a page that neither carries
// the marker nor delegates still FAILS (pinned by
// `delegation-detection-is-not-an-escape-hatch`).

const DATA_TABLE_PAGE = resolve(DASHBOARDS, "shared", "data-table-page.tsx")
const RESPONSIVE_DATA_TABLE = resolve(
  DASHBOARDS,
  "shared",
  "responsive-data-table.tsx",
)
const DASHBOARD_HEADER = resolve(DASHBOARDS, "shared", "dashboard-header.tsx")

const SCAFFOLD_IMPORT_RE =
  /from\s+["']@\/components\/dashboard\/shared\/data-table-page["']/

/** True if this page renders its list through `<DataTablePage>`.
 *
 * Requires BOTH the import and an actual `<DataTablePage` render, so
 * a stray type-only import can't be used to opt out of the audit.
 */
function delegatesToScaffold(src: string): boolean {
  return SCAFFOLD_IMPORT_RE.test(src) && src.includes("<DataTablePage")
}

// ---------------------------------------------------------------------------
// CC-1 / CC-2 — Tasks dashboard palette migration to theme tokens
// ---------------------------------------------------------------------------

const TASKS = resolve(DASHBOARDS, "tasks-dashboard.tsx")

// Patterns the audit identified as theme-bypass hardcodes. Each is a
// substring search; if even one appears anywhere in tasks-dashboard.tsx,
// the migration is incomplete.
const TASKS_FORBIDDEN_SUBSTRINGS = [
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

function countOccurrences(haystack: string, needle: string): number {
  let count = 0
  let idx = haystack.indexOf(needle)
  while (idx !== -1) {
    count++
    idx = haystack.indexOf(needle, idx + needle.length)
  }
  return count
}

describe("CC-1/CC-2: tasks dashboard palette migration", () => {
  it("uses shadcn theme tokens, not raw Tailwind palette classes", () => {
    // Tasks dashboard must use shadcn semantic tokens (bg-card,
    // text-foreground, border-border, text-primary, focus-visible:ring-ring,
    // etc.) instead of raw Tailwind palette classes that bypass the theme
    // system and duplicate light/dark variants inline.
    //
    // The audit counted 40 hits in this single file (the only dashboard
    // that does this). Migrating to tokens both fixes the modern-minimal
    // aesthetic violation AND makes a future theme tweak a one-place
    // change in tailwind.config.ts / globals.css.
    const src = read(TASKS)
    const hits: Record<string, number> = {}
    for (const sub of TASKS_FORBIDDEN_SUBSTRINGS) {
      const n = countOccurrences(src, sub)
      if (n) hits[sub] = n
    }
    expect(
      Object.keys(hits),
      "tasks-dashboard.tsx still contains theme-bypass palette " +
        "hardcodes (CC-1/CC-2): " +
        Object.entries(hits)
          .sort()
          .map(([k, v]) => `${JSON.stringify(k)}×${v}`)
          .join(", ") +
        ". Migrate to shadcn semantic tokens: bg-card / bg-muted / " +
        "border-border / text-foreground / text-muted-foreground / " +
        "text-primary / border-primary/20 / focus-visible:ring-ring.",
    ).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// CC-14 — every <DialogContent> needs the mobile-width fallback
// ---------------------------------------------------------------------------

// Audit surface for CC-14, enumerated DYNAMICALLY.
//
// This used to be a hardcoded list of 13 paths. That list was a drift
// source in both directions: it silently skipped app dialogs nobody
// remembered to add (it was missing 9 files carrying `<DialogContent>`,
// one of which had a real un-audited violation), and it hard-FAILED the
// whole suite when a listed file was legitimately deleted — which is
// exactly what happened when `delete-memory-modal.tsx` was subsumed by
// the unified `<DeleteConfirmModal>`. A file list that breaks on a
// legitimate deletion is testing the manifest, not the code.
//
// Globbing the component tree instead means "every dialog we ship" is
// the audit surface, automatically, forever.
//
// `components/ui/` is excluded: those are vendored shadcn primitives
// (e.g. `command.tsx`'s CommandDialog), not app-authored dialogs — the
// app-level wrappers around them ARE covered.
const DIALOG_CONTENT_ROOTS = [DASHBOARDS, SERVER_DIR]

function dialogContentFiles(): string[] {
  const found: string[] = []
  for (const root of DIALOG_CONTENT_ROOTS) {
    for (const f of rglob(root, ".tsx").sort()) {
      if (f.endsWith(".test.tsx")) continue
      if (read(f).includes("<DialogContent")) found.push(f)
    }
  }
  return found
}

// Match `<DialogContent ... className="..."` with the className value
// captured. Multi-line tolerant.
const DIALOG_CONTENT_RE = /<DialogContent\b[^>]*?\bclassName=\{?"([^"]*)"/gs

// Every `<DialogContent` opening tag, className or not.
//
// The className-anchored pattern above cannot see a dialog written as a
// bare `<DialogContent>` — it simply does not match, so the site is
// skipped SILENTLY. That is not a theoretical hole: the schedules page
// shipped two of them, and its delete confirm sat outside this audit
// (while clipping on a phone, which is exactly what the audit exists to
// catch) for as long as it existed. A classless DialogContent has no
// mobile-width fallback BY CONSTRUCTION, so it is always a violation —
// counting it as one is what makes the audit's surface equal to its
// glob.
const DIALOG_CONTENT_ANY_RE = /<DialogContent\b/g

// Comments legitimately WRITE `<DialogContent>` when explaining this
// very audit, so both passes read comment-stripped source. Otherwise a
// doc-comment counts as a dialog and the classless check reports files
// that are actually fine.
const JSX_COMMENT_RE = /\{\/\*[\s\S]*?\*\/\}/g
const BLOCK_COMMENT_RE = /\/\*[\s\S]*?\*\//g
const LINE_COMMENT_RE = /^\s*\/\/.*$/gm

function codeOnly(src: string): string {
  src = src.replace(JSX_COMMENT_RE, "")
  src = src.replace(BLOCK_COMMENT_RE, "")
  return src.replace(LINE_COMMENT_RE, "")
}

describe("CC-14: DialogContent mobile-width fallback", () => {
  it("every <DialogContent> has w-[calc(100vw-2rem)]", () => {
    // Every <DialogContent> must include `w-[calc(100vw-2rem)]` so it
    // fits with a 1-rem-each-side gutter on 375 px viewports. Without the
    // fallback, dialogs widen to whatever their `sm:max-w-*` says even on
    // sub-sm viewports and clip/overflow horizontally.
    //
    // Pattern was established by:
    //   * PR #54 (tasks View dialog layout polish)
    //   * PR #49 (tasks-page row-body click View dialog)
    //   * PR #65 (agent popup MCP-onboarding tabs + polish)
    //
    // Sweep applies the same fix to the remaining 18 DialogContent
    // usages found by the audit.
    //
    // The file set is globbed (see `dialogContentFiles`), so a dialog
    // added tomorrow is audited automatically and a dialog legitimately
    // deleted doesn't red the suite.
    const files = dialogContentFiles()
    expect(
      files.length,
      "no <DialogContent> sites found under " +
        JSON.stringify(DIALOG_CONTENT_ROOTS) +
        " — the glob is broken, which would silently disable this audit",
    ).toBeGreaterThan(0)
    const failures: string[] = []
    for (const f of files) {
      const src = codeOnly(read(f))
      const anchoredRe = new RegExp(DIALOG_CONTENT_RE.source, "gs")
      let m: RegExpExecArray | null
      let seen = 0
      while ((m = anchoredRe.exec(src)) !== null) {
        seen++
        const classes = m[1]!
        if (!classes.includes("w-[calc(100vw-2rem)]")) {
          const line = src.slice(0, m.index).split("\n").length
          const snippet =
            classes.slice(0, 80) + (classes.length > 80 ? "…" : "")
          failures.push(`${f}:${line}  className=${JSON.stringify(snippet)}`)
        }
      }
      // Classless tags: every `<DialogContent` must have been reached
      // by the className-anchored pass above. Any surplus is a dialog
      // this audit could not see.
      const anyRe = new RegExp(DIALOG_CONTENT_ANY_RE.source, "g")
      const total = (src.match(anyRe) ?? []).length
      if (total > seen) {
        failures.push(
          `${f}  ${total - seen} <DialogContent> with NO className ` +
            "(invisible to this audit, and with no mobile-width " +
            "fallback by construction)",
        )
      }
    }
    expect(
      failures,
      "DialogContent missing mobile-width fallback " +
        "`w-[calc(100vw-2rem)]` at " +
        failures.length +
        " sites:\n  " +
        failures.join("\n  "),
    ).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Dialog mobile height-cap — every <DialogContent> needs `dvh`, not `vh`
// ---------------------------------------------------------------------------
//
// Companion to CC-14 above, added after a real production bug: the
// schedules create/edit dialog (and several siblings — DeleteConfirmModal,
// project-memberships, users, add/remove/rename-project, send-directive)
// had no height cap at all, so a tall form's bottom (fields + action
// buttons) got clipped behind mobile Safari/Chrome's collapsing address
// bar with no way to scroll to it. `vh` alone doesn't fix this — it's
// computed against the LARGEST possible viewport, not the one visible
// once the browser chrome collapses; `dvh` (dynamic viewport height)
// tracks the real visible area. See
// docs/learnings/dashboard-dialog-mobile-clipping.md.
//
// Reuses the same glob as CC-14 (`dialogContentFiles`) so a dialog
// added tomorrow is covered automatically, and treats the `<form>`-wrapped
// dialogs' outer DialogContent the same way as the plain ones — the cap
// must be on DialogContent itself, not just somewhere in the file.

describe("CC-14 companion: DialogContent mobile height cap", () => {
  it("every <DialogContent> caps height with a dvh unit", () => {
    // Every <DialogContent> must include a `dvh`-based height cap
    // (`max-h-[...dvh...]`) so mobile Safari/Chrome's collapsing address
    // bar can't clip its bottom content with no way to scroll to it.
    //
    // `vh` alone is NOT sufficient (see module docstring) — pin the
    // stronger unit explicitly rather than just "some height cap exists".
    const files = dialogContentFiles()
    expect(
      files.length,
      "no <DialogContent> sites found under " +
        JSON.stringify(DIALOG_CONTENT_ROOTS) +
        " — the glob is broken, which would silently disable this audit",
    ).toBeGreaterThan(0)
    const dvhRe = /max-h-\[[^\]]*dvh[^\]]*\]/
    const failures: string[] = []
    for (const f of files) {
      const src = codeOnly(read(f))
      const anchoredRe = new RegExp(DIALOG_CONTENT_RE.source, "gs")
      let m: RegExpExecArray | null
      while ((m = anchoredRe.exec(src)) !== null) {
        const classes = m[1]!
        if (!dvhRe.test(classes)) {
          const line = src.slice(0, m.index).split("\n").length
          const snippet =
            classes.slice(0, 80) + (classes.length > 80 ? "…" : "")
          failures.push(`${f}:${line}  className=${JSON.stringify(snippet)}`)
        }
      }
    }
    expect(
      failures,
      "DialogContent missing a dvh-based height cap " +
        "(`max-h-[calc(100dvh-2rem)]` or similar) at " +
        failures.length +
        " sites — mobile Safari/Chrome's collapsing address bar will " +
        "clip the bottom of these dialogs with no way to scroll to it:\n  " +
        failures.join("\n  "),
    ).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// CC-7 — mobile card-list sibling for every data-table dashboard
// ---------------------------------------------------------------------------

function tableDashboards(): Record<string, string> {
  // Derive the CC-7 audit set instead of hardcoding paths.
  //
  // A page is in scope if it either ships a `*-mobile-list.tsx` sibling
  // (the pre-scaffold idiom) or renders through `<DataTablePage>` (the
  // scaffold idiom). Both halves are computed from the tree, so a new
  // list page or a new migration joins the audit with no edit here, and
  // deleting a file can't break the manifest.
  const targets: Record<string, string> = {}
  const mobileFiles = readdirSync(DASHBOARDS)
    .filter((f) => f.endsWith("-mobile-list.tsx"))
    .sort()
  for (const mobile of mobileFiles) {
    const slug = mobile.slice(0, -"-mobile-list.tsx".length)
    const page = resolve(DASHBOARDS, `${slug}-dashboard.tsx`)
    try {
      if (statSync(page).isFile()) targets[slug] = page
    } catch {
      // page sibling doesn't exist — not in scope.
    }
  }
  const pageFiles = readdirSync(DASHBOARDS)
    .filter((f) => f.endsWith("-dashboard.tsx"))
    .sort()
  for (const pageFile of pageFiles) {
    const page = resolve(DASHBOARDS, pageFile)
    if (delegatesToScaffold(read(page))) {
      targets[pageFile.slice(0, -"-dashboard.tsx".length)] = page
    }
  }
  return targets
}

describe("CC-7: mobile card-list sibling for data-table dashboards", () => {
  it("every table dashboard has a mobile card-list or delegates to the scaffold", () => {
    // At < sm: viewports the desktop <Table> renders are unusable
    // (5-7 columns × 10+ rows horizontally overflow on 375 px). Each
    // list dashboard must therefore offer a mobile card alternative —
    // satisfied EITHER directly (import a `*-mobile-list` sibling and
    // render the `hidden sm:block` / `block sm:hidden` twin guard) OR by
    // delegating to `<DataTablePage>`, which renders the twin guard
    // inside `<ResponsiveDataTable>` from one column spec.
    //
    // The scaffold's own guarantee is pinned by
    // `responsive-data-table-renders-mobile-twin-guard`, so the
    // delegation branch is an equivalence, not an exemption.
    const targets = tableDashboards()
    expect(
      Object.keys(targets).length,
      "CC-7 audit set derived empty — the derivation is broken",
    ).toBeGreaterThan(0)
    const failures: string[] = []
    for (const [slug, path] of Object.entries(targets)) {
      const src = read(path)
      // Branch 1 — the page delegates the whole table shell.
      if (delegatesToScaffold(src)) continue
      // Branch 2 — the page renders its own table + mobile twin.
      const mobileImportRe = new RegExp(
        `from\\s+["']@/components/dashboard/${slug}-mobile-list["']`,
      )
      if (!mobileImportRe.test(src)) {
        failures.push(
          `${path}: neither renders via <DataTablePage> nor imports ` +
            `'@/components/dashboard/${slug}-mobile-list'`,
        )
        continue
      }
      // Look for the twin-guard idiom: hidden sm:block AND sm:hidden.
      if (!src.includes("hidden sm:block")) {
        failures.push(
          `${path}: no \`hidden sm:block\` class (table-only guard) found`,
        )
      }
      if (!src.includes("sm:hidden")) {
        failures.push(
          `${path}: no \`sm:hidden\` class (mobile-only guard) found`,
        )
      }
    }
    expect(
      failures,
      "Mobile card-list conversion incomplete (CC-7):\n  " +
        failures.join("\n  "),
    ).toEqual([])
  })
})

// `tableDashboards()` above is deliberately scoped to full-page list
// dashboards (`*-dashboard.tsx` + their `*-mobile-list.tsx` sibling
// convention) — the CC-7 bar for those is a genuine mobile card-list
// rewrite, which is real migration work, not something a raw-table file
// should be silently held to.
//
// But that same filename-suffix gate means a raw `<Table>` hand-rolled
// inside a MODAL (not a page) is invisible to the whole file — audited
// nowhere. `project-memberships-modal.tsx` was exactly this: found
// during the prancy-napping-pie sweep with a 4-column `<Table>` and no
// horizontal-scroll safety net at all, sitting in a dialog capped to
// `w-[calc(100vw-2rem)]`. The bar for a dialog-hosted table is lower
// than CC-7's — it doesn't need its own card-list twin, it needs to not
// clip: wrapped in `overflow-x-auto` so an overflowing row scrolls
// instead of blowing out the dialog.
function dialogHostedTableFiles(): string[] {
  // Every `.tsx` under `components/dashboard/` with a raw `<Table>`
  // render, EXCLUDING the full-page dashboards already covered by
  // `tableDashboards()`'s stricter CC-7 mobile-card-list bar.
  const found: string[] = []
  for (const f of rglob(DASHBOARDS, ".tsx").sort()) {
    if (f.endsWith(".test.tsx")) continue
    if (f.endsWith("-dashboard.tsx")) continue
    const src = read(f)
    if (src.includes("@/components/ui/table") && src.includes("<Table")) {
      found.push(f)
    }
  }
  return found
}

describe("Dialog-hosted raw <Table> scroll safety", () => {
  it("scrolls horizontally instead of clipping", () => {
    // A raw `<Table>` rendered inside a modal must be wrapped in an
    // `overflow-x-auto` container. Unlike a full dashboard page, a dialog
    // has no room to grow — CC-14 already caps its width at
    // `w-[calc(100vw-2rem)]`, so an unwrapped table with more columns
    // than fit clips silently with no way to reach the rest of the row.
    const files = dialogHostedTableFiles()
    expect(
      files.length,
      "dialog-hosted-table audit set derived empty — " +
        "the derivation is broken",
    ).toBeGreaterThan(0)
    const failures: string[] = []
    for (const f of files) {
      const src = codeOnly(read(f))
      const m = /<Table\b/.exec(src)
      expect(m, f).not.toBeNull()
      // The nearest wrapping `overflow-x-auto` div is expected
      // immediately around the `<Table>` — a simple presence check
      // in the same file is enough to catch the "no wrapper at all"
      // case this test exists for, without needing a real JSX parse.
      if (!src.includes("overflow-x-auto")) {
        const line = src.slice(0, m!.index).split("\n").length
        failures.push(`${f}:${line}`)
      }
    }
    expect(
      failures,
      "Raw <Table> in a dialog with no overflow-x-auto wrapper — " +
        "will clip columns with no way to scroll to them on a " +
        "CC-14-capped dialog width:\n  " + failures.join("\n  "),
    ).toEqual([])
  })
})

describe("CC-7 scaffold half: ResponsiveDataTable", () => {
  it("renders the mobile twin guard itself", () => {
    // The scaffold half of CC-7: `<ResponsiveDataTable>` must itself
    // render the desktop/mobile twin guard, because every page that
    // delegates inherits its mobile behaviour from exactly this file.
    expect(
      statSync(RESPONSIVE_DATA_TABLE).isFile(),
      `expected the shared responsive table at ${RESPONSIVE_DATA_TABLE}`,
    ).toBe(true)
    const src = read(RESPONSIVE_DATA_TABLE)
    const missing = ["hidden sm:block", "sm:hidden"].filter(
      (c) => !src.includes(c),
    )
    expect(
      missing,
      `${RESPONSIVE_DATA_TABLE}: missing twin-guard class(es) ` +
        `${JSON.stringify(missing)}. Every <DataTablePage> page inherits its mobile ` +
        "card-list from this component (CC-7).",
    ).toEqual([])
  })
})

describe("CC-7 scaffold half: DataTablePage", () => {
  it("routes rows through ResponsiveDataTable", () => {
    // `<DataTablePage>` must actually route its rows through
    // `<ResponsiveDataTable>` — otherwise the CC-7 delegation branch
    // above would be vacuous.
    expect(
      statSync(DATA_TABLE_PAGE).isFile(),
      `expected the shared list-page scaffold at ${DATA_TABLE_PAGE}`,
    ).toBe(true)
    const src = read(DATA_TABLE_PAGE)
    expect(
      src.includes("<ResponsiveDataTable"),
      `${DATA_TABLE_PAGE}: does not render <ResponsiveDataTable>. ` +
        "Pages delegating to the scaffold would then have no mobile " +
        "card-list at all (CC-7).",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-3 — Skeleton loading
// ---------------------------------------------------------------------------

describe("CC-3: skeleton loading", () => {
  it("every list dashboard uses Skeleton loading (directly, sub-component, or delegated)", () => {
    // Each list dashboard (tasks, agents, messages, memories,
    // prompt-book) must render a `Skeleton` loading state — satisfied by
    // a direct `@/components/ui/skeleton` import, a per-page
    // `*-loading` sub-component that imports it, OR delegation to
    // `<DataTablePage>` (which owns the stats+rows skeleton for every
    // page that renders through it — pinned by
    // `data-table-page-provides-skeleton-loading`).
    //
    // Before any of these existed every page fell back to "Loading…"
    // text or a blank pane, which is sloppy; the shadcn primitive
    // shipped in the repo but was unused.
    const targets: Record<string, string> = {
      tasks: resolve(DASHBOARDS, "tasks-dashboard.tsx"),
      agents: resolve(DASHBOARDS, "agents-dashboard.tsx"),
      messages: resolve(DASHBOARDS, "messages-dashboard.tsx"),
      memories: resolve(DASHBOARDS, "memories-dashboard.tsx"),
      "prompt-book": resolve(DASHBOARDS, "prompt-book-dashboard.tsx"),
    }
    const failures: string[] = []
    for (const [slug, path] of Object.entries(targets)) {
      const src = read(path)
      const direct = new RegExp(
        `from\\s+["']@/components/ui/skeleton["']`,
      ).test(src)
      // OR: imports a per-page loading sub-component that uses Skeleton.
      const loadingSubImport = new RegExp(
        `from\\s+["']@/components/dashboard/${slug}-loading["']`,
      ).test(src)
      let loadingSubUsesSkeleton = false
      if (loadingSubImport) {
        const loadingPath = resolve(DASHBOARDS, `${slug}-loading.tsx`)
        try {
          if (statSync(loadingPath).isFile()) {
            const loadingSrc = read(loadingPath)
            loadingSubUsesSkeleton = /from\s+["']@\/components\/ui\/skeleton["']/.test(
              loadingSrc,
            )
          }
        } catch {
          // no sub-component file — leave false.
        }
      }
      if (!(direct || loadingSubUsesSkeleton || delegatesToScaffold(src))) {
        failures.push(
          `${path}: no direct Skeleton import, no ${slug}-loading ` +
            "sub-component using Skeleton, and no <DataTablePage> " +
            "delegation",
        )
      }
    }
    expect(
      failures,
      "Skeleton loading missing (CC-3):\n  " + failures.join("\n  "),
    ).toEqual([])
  })
})

describe("CC-3 scaffold half: DataTablePage provides skeleton loading", () => {
  it("imports and renders Skeleton", () => {
    // The scaffold half of CC-3: every page delegating to
    // `<DataTablePage>` inherits its loading state from this file, so
    // the file must actually import and render the Skeleton primitive.
    const src = read(DATA_TABLE_PAGE)
    expect(
      /from\s+["']@\/components\/ui\/skeleton["']/.test(src),
      `${DATA_TABLE_PAGE}: does not import the Skeleton primitive. ` +
        "Pages delegating to the scaffold would then have no skeleton " +
        "loading state (CC-3).",
    ).toBe(true)
    expect(
      src.includes("<Skeleton"),
      `${DATA_TABLE_PAGE}: imports Skeleton but never renders it (CC-3).`,
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-6 — shared <EmptyState> primitive used everywhere
// ---------------------------------------------------------------------------

const EMPTY_STATE_PRIMITIVE = resolve(DASHBOARDS, "shared", "empty-state.tsx")

describe("CC-6: shared EmptyState primitive", () => {
  it("exists", () => {
    expect(
      statSync(EMPTY_STATE_PRIMITIVE).isFile(),
      `expected shared EmptyState primitive at ${EMPTY_STATE_PRIMITIVE}. ` +
        "Pulls the duplicated per-page empty-state markup into one " +
        "place + lets us drop the tasks-dashboard slate/teal " +
        "anti-pattern as a side-effect (CC-1 overlap).",
    ).toBe(true)
  })
})

const EMPTY_STATE_IMPORT_RE =
  /from\s+["']@\/components\/dashboard\/shared\/empty-state["']/

describe("CC-6/CC-20: list dashboards import EmptyState", () => {
  it("every list dashboard imports EmptyState (directly or via delegation)", () => {
    // Each list dashboard + messages dashboard renders the shared
    // EmptyState primitive (CC-6, CC-20) — satisfied either by importing
    // it directly or by delegating to `<DataTablePage>`, which renders
    // `<EmptyState>` for its `empty` prop (pinned by
    // `data-table-page-provides-empty-state`).
    const targets = [
      resolve(DASHBOARDS, "tasks-dashboard.tsx"),
      resolve(DASHBOARDS, "agents-dashboard.tsx"),
      resolve(DASHBOARDS, "memories-dashboard.tsx"),
      resolve(DASHBOARDS, "messages-dashboard.tsx"),
      resolve(DASHBOARDS, "prompt-book-dashboard.tsx"),
    ]
    const failures: string[] = []
    for (const path of targets) {
      const src = read(path)
      if (!(EMPTY_STATE_IMPORT_RE.test(src) || delegatesToScaffold(src))) {
        failures.push(path)
      }
    }
    expect(
      failures,
      "EmptyState primitive not imported (CC-6/CC-20):\n  " +
        failures.join("\n  "),
    ).toEqual([])
  })
})

describe("Guard the guard: delegation detection is not an escape hatch", () => {
  it("correctly classifies delegating vs non-delegating pages", () => {
    // The CC-3 / CC-6 / CC-7 tests accept "renders via <DataTablePage>"
    // in place of an inline marker. That is only sound while
    // `delegatesToScaffold` stays strict, so pin its negative cases:
    // a page with no scaffold reference, and a page that imports the
    // scaffold type but never renders it, must BOTH be treated as
    // non-delegating and therefore still be required to carry their own
    // markers.
    expect(
      delegatesToScaffold("export function X(){ return <div>plain page</div> }"),
      "a page with no scaffold reference must not count as delegating",
    ).toBe(false)

    const importOnly =
      "import type { DataTablePageProps } from " +
      "'@/components/dashboard/shared/data-table-page'\n" +
      "export function X(){ return <div>hand-rolled</div> }"
    expect(
      delegatesToScaffold(importOnly),
      "importing the scaffold without rendering <DataTablePage> must " +
        "not satisfy the audit — otherwise a page could opt out of " +
        "CC-3/CC-6/CC-7 with a single unused import",
    ).toBe(false)

    const renderOnly = "export function X(){ return <DataTablePage /> }"
    expect(
      delegatesToScaffold(renderOnly),
      "a <DataTablePage> render with no matching import must not " +
        "count as delegating",
    ).toBe(false)

    const real =
      "import { DataTablePage } from " +
      "'@/components/dashboard/shared/data-table-page'\n" +
      "export function X(){ return <DataTablePage rows={[]} /> }"
    expect(
      delegatesToScaffold(real),
      "a genuine import + render must be recognised as delegating",
    ).toBe(true)

    // And the audit must still be watching at least one page that does
    // NOT delegate — otherwise every assertion above is vacuous and the
    // per-page markers would go unchecked repo-wide.
    const nonDelegating = readdirSync(DASHBOARDS)
      .filter((f) => f.endsWith("-dashboard.tsx"))
      .sort()
      .map((f) => resolve(DASHBOARDS, f))
      .filter((p) => !delegatesToScaffold(read(p)))
    expect(
      nonDelegating.length,
      "every dashboard now delegates — re-point these audits at the " +
        "shared components directly instead of leaving them vacuous",
    ).toBeGreaterThan(0)
  })
})

describe("CC-6/CC-20 scaffold half: DataTablePage provides EmptyState", () => {
  it("imports and renders EmptyState", () => {
    // The scaffold half of CC-6/CC-20: pages delegating to
    // `<DataTablePage>` inherit their empty state from this file.
    const src = read(DATA_TABLE_PAGE)
    expect(
      EMPTY_STATE_IMPORT_RE.test(src),
      `${DATA_TABLE_PAGE}: does not import the shared EmptyState ` +
        "primitive. Pages delegating to the scaffold would then have no " +
        "empty state (CC-6/CC-20).",
    ).toBe(true)
    expect(
      src.includes("<EmptyState"),
      `${DATA_TABLE_PAGE}: imports EmptyState but never renders it ` +
        "(CC-6/CC-20).",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-9 — header per-page title
// ---------------------------------------------------------------------------

const HEADER = resolve(LAYOUT_DIR, "header.tsx")

describe("CC-9: header renders a per-page title", () => {
  it("references currentView", () => {
    // The header must surface the current page name so mobile users
    // have a breadcrumb once the sidebar sheet closes. Look for any
    // reference to `currentView` (the dashboard store hook that knows
    // which page is active).
    const src = read(HEADER)
    expect(
      src.includes("currentView"),
      `${HEADER}: no \`currentView\` reference. Header should derive a ` +
        "page title from the dashboard store so mobile users have a " +
        "breadcrumb when the sidebar is closed.",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-15 — drop animate-fade-in from main layout
// ---------------------------------------------------------------------------

const MAIN_LAYOUT = resolve(LAYOUT_DIR, "main-layout.tsx")

describe("CC-15: main layout drops animate-fade-in", () => {
  it("no longer wraps every page render in animate-fade-in", () => {
    // `animate-fade-in` on every page render combined with shadcn
    // dialog enters and sidebar tooltip animations reads as busy
    // motion. Modern-minimal calls for component-level transitions
    // (150 ms ease on hover/focus) — let those own motion. Drop the
    // layout-level fade.
    const src = read(MAIN_LAYOUT)
    expect(
      src.includes("animate-fade-in"),
      `${MAIN_LAYOUT}: \`animate-fade-in\` should be removed from the ` +
        "main-content wrapper. Layout-level page-fade animation is " +
        "noisy combined with the rest of the UI's motion.",
    ).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// CC-17 — drop hard-coded text-blue-800 in prompt-book
// ---------------------------------------------------------------------------

const PROMPT_BOOK = resolve(DASHBOARDS, "prompt-book-dashboard.tsx")

describe("CC-17: prompt-book drops hardcoded blue", () => {
  it("no longer contains text-blue-800", () => {
    const src = read(PROMPT_BOOK)
    expect(
      src.includes("text-blue-800"),
      `${PROMPT_BOOK}: \`text-blue-800\` is a theme-bypass hardcode. ` +
        "Switch to `text-foreground` on a `bg-muted/50` container — " +
        "matches the modern-minimal monochrome palette + works under " +
        "both light and dark themes.",
    ).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// CC-10 — sidebar footer no longer says "Improved Dashboard"
// ---------------------------------------------------------------------------

const APP_SIDEBAR = resolve(LAYOUT_DIR, "app-sidebar.tsx")

describe("CC-10: sidebar drops the Improved Dashboard tagline", () => {
  it("no longer contains 'Improved Dashboard'", () => {
    const src = read(APP_SIDEBAR)
    expect(
      src.includes("Improved Dashboard"),
      `${APP_SIDEBAR}: the \`Improved Dashboard\` tagline reads as ` +
        "leftover beta-marketing copy. Drop it — show just the " +
        "product name + version.",
    ).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// CC-18 — settings policy rows reflow on mobile
// ---------------------------------------------------------------------------

const SETTINGS = resolve(DASHBOARDS, "settings-dashboard.tsx")

describe("CC-18: settings policy rows reflow on mobile", () => {
  it("uses flex-col sm:flex-row", () => {
    // Settings policy rows pair a long description with a Switch on
    // the right via `flex items-start justify-between`. At 375 px the
    // description squashes the Switch. Apply `flex-col sm:flex-row`
    // so the Switch drops below the description on mobile.
    const src = read(SETTINGS)
    expect(
      src.includes("flex-col sm:flex-row"),
      `${SETTINGS}: no \`flex-col sm:flex-row\` reflow class found. ` +
        "Policy / retention rows should stack vertically at < sm: and " +
        "lay out horizontally at >= sm:.",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-20 — messages page imports EmptyState (covered by
// list-dashboards-import-empty-state above)
// ---------------------------------------------------------------------------
// Explicit dedicated test in case the broader assertion changes:

describe("CC-20: messages dashboard imports EmptyState explicitly", () => {
  it("imports the shared EmptyState primitive", () => {
    const src = read(resolve(DASHBOARDS, "messages-dashboard.tsx"))
    expect(
      EMPTY_STATE_IMPORT_RE.test(src),
      "messages-dashboard.tsx must import the shared EmptyState " +
        "primitive (CC-20). Today the page renders `0 messages` in " +
        "the CardHeader with no empty body — users get a confusing " +
        "blank table region. Render <EmptyState> when " +
        "filteredMessages.length === 0.",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-25 — mobile sidebar Sheet auto-closes on nav-item click
// ---------------------------------------------------------------------------

const NAVIGATION = resolve(LAYOUT_DIR, "navigation.tsx")

describe("CC-25: mobile sidebar Sheet auto-closes on nav click", () => {
  it("calls setOpenMobile", () => {
    // When the mobile Sheet sidebar is open, clicking a nav item must
    // auto-close the Sheet (iOS-style navigation UX). Currently the user
    // has to manually dismiss the Sheet after picking the page.
    //
    // Wire `setOpenMobile(false)` (from `useSidebar()` in the shadcn
    // Sidebar primitive) into the NavButton onClick handler.
    const src = read(NAVIGATION)
    expect(
      src.includes("setOpenMobile"),
      `${NAVIGATION}: no \`setOpenMobile\` reference. Navigation must ` +
        "close the mobile Sheet after a nav-item click; otherwise the " +
        "user is stranded with the sheet still over their content " +
        "after they pick a page (verified at 375 px in the audit " +
        "screenshots).",
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-24 — Prompt Book Tabs deduplication (no two TabsTrigger with same value)
// ---------------------------------------------------------------------------

describe("CC-24: prompt-book tabs do not truncate to first word", () => {
  it("does not render category.name.split(' ')[0]", () => {
    // The Prompt Book Tabs render `{category.name.split(' ')[0]}`
    // which truncates "Agent Initialization" and "Agent Coordination"
    // both to "Agent" — verified in the 375 px audit screenshot
    // where two adjacent tabs both read "Agent".
    //
    // Fix: render the full `category.name` (and use overflow-x-auto
    // on the TabsList so the tabs scroll horizontally on mobile).
    const src = read(PROMPT_BOOK)
    // Locate the TabsTrigger block and inspect its inner content.
    // Look for `category.name.split(' ')[0]` exactly — that's the bug.
    expect(
      src.includes("category.name.split(' ')[0]"),
      `${PROMPT_BOOK}: TabsTrigger renders \`category.name.` +
        "split(' ')[0]\` — splits multi-word category names (" +
        "'Agent Initialization', 'Agent Coordination') to a " +
        "single ambiguous first word ('Agent', 'Agent'). Render the " +
        "full `category.name` and let the TabsList overflow-x-auto " +
        "handle narrow viewports.",
    ).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// CC-23 — Prompt Book header action buttons stack on mobile
// ---------------------------------------------------------------------------

describe("CC-23: prompt-book header actions reflow on mobile", () => {
  it("wraps the action-group container in flex-wrap", () => {
    // The Prompt Book page header action group (count badges +
    // Create Prompt + Help buttons) is wrapped in a `flex items-center
    // gap-2` container with NO `flex-wrap`. At 375 px the inner row
    // overflows the right edge (visible in the audit screenshot —
    // "Create Prompt" cut to "Compo" then "Help" cut entirely).
    //
    // The outer header IS already `flex-col sm:flex-row` (line 401),
    // so the action group drops to its own row on mobile — but within
    // that row, the 4 badges + 2 buttons still need `flex-wrap` to
    // avoid horizontal overflow.
    //
    // Pin: the action group container near "Create Prompt" must have
    // `flex-wrap` somewhere in its className.
    const src = read(PROMPT_BOOK)
    // Find the Create Prompt button anchor.
    const cpIdx = src.indexOf("Create Prompt")
    expect(cpIdx, "Create Prompt button text not found").toBeGreaterThanOrEqual(0)
    // Walk backwards to find the wrapping <div className="...">.
    const regionStart = src.lastIndexOf('<div className="', cpIdx)
    expect(regionStart, "couldn't locate action-group container").toBeGreaterThanOrEqual(0)
    const regionEnd = src.indexOf('">', regionStart) + 2
    const containerTag = src.slice(regionStart, regionEnd)
    expect(
      containerTag.includes("flex-wrap"),
      `${PROMPT_BOOK}: the action-group div containing the ` +
        "Create Prompt button must include `flex-wrap` so the 4 " +
        "badges + 2 buttons can break to multiple rows on narrow " +
        "viewports (audit found horizontal overflow at 375 px). " +
        `Current container tag:\n  ${containerTag}`,
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// CC-21 — Tasks H1 visible in light mode (root cause is the same
// `text-white dark:text-white text-slate-900` anti-pattern covered
// by CC-1 forbidden substrings — Tailwind v4 emits utilities in
// alphabetical not source order, so `text-white` ships AFTER
// `text-slate-900` in the stylesheet → always-on white wins and
// the H1 vanishes on light backgrounds). Explicit regression test:
// ---------------------------------------------------------------------------

/** Locate the `<h1>` wrapping `title` and report whether it carries
 * `text-foreground`. Returns [ok, detail] — detail is the offending
 * tag (or a not-found note) for the failure message.
 */
function h1UsesTextForeground(src: string, title: string): [boolean, string] {
  const titleIdx = src.indexOf(title)
  if (titleIdx < 0) return [false, `H1 text ${JSON.stringify(title)} not found`]
  const regionStart = src.lastIndexOf("<h1", titleIdx)
  if (regionStart < 0) return [false, `no <h1> tag precedes ${JSON.stringify(title)}`]
  const regionEnd = src.indexOf(">", regionStart) + 1
  if (regionEnd <= regionStart) {
    return [false, `unterminated <h1> tag before ${JSON.stringify(title)}`]
  }
  const h1Tag = src.slice(regionStart, regionEnd)
  return [h1Tag.includes("text-foreground"), h1Tag.slice(0, 200)]
}

describe("CC-21: tasks H1 uses text-foreground", () => {
  it("Tasks H1 uses text-foreground (directly or via DataTablePage header title)", () => {
    // The Tasks page H1 once read `text-fluid-2xl font-bold
    // text-white dark:text-white text-slate-900` — Tailwind v4 emits
    // `text-white` AFTER `text-slate-900` in the generated stylesheet
    // (utilities are ordered, not source-order), so the always-on
    // `text-white` wins over the always-on `text-slate-900` and the
    // H1 disappears on light-mode white backgrounds. It must use
    // `text-foreground` (semantic token).
    //
    // Delegation-aware, same equivalence as CC-3/CC-6/CC-7: the page
    // satisfies this either by rendering its own `<h1>` or by handing the
    // title to `<DataTablePage header={{title: ...}}>`, whose
    // `<DashboardHeader>` renders the `<h1>`. The delegating branch is
    // NOT an exemption — it additionally requires that the page really
    // passes "Task Operations" as the header title, and the scaffold half
    // is pinned directly by
    // `dashboard-header-h1-uses-text-foreground`.
    const src = read(TASKS)
    if (delegatesToScaffold(src)) {
      expect(
        /title:\s*['"]Task Operations['"]/.test(src),
        `${TASKS}: delegates to <DataTablePage> but does not pass ` +
          "`title: 'Task Operations'` in its `header` prop — the page " +
          "would then have no H1 at all (CC-21).",
      ).toBe(true)
      return
    }
    const [ok, detail] = h1UsesTextForeground(src, "Task Operations")
    expect(
      ok,
      `${TASKS}: Tasks H1 must use \`text-foreground\` (semantic ` +
        "token), not the broken `text-white dark:text-white " +
        "text-slate-900` triplet that resolves to invisible white " +
        `text in light mode. Current H1 tag:\n  ${detail}`,
    ).toBe(true)
  })
})

describe("CC-21 scaffold half: DashboardHeader H1 uses text-foreground", () => {
  it("shared header H1 uses text-foreground", () => {
    // The scaffold half of CC-21: every page that hands its title to
    // `<DashboardHeader>` (directly or via `<DataTablePage>`) inherits
    // its H1 styling from exactly this file, so the shared header must
    // itself use the semantic token.
    const src = read(DASHBOARD_HEADER)
    const [ok, detail] = h1UsesTextForeground(src, "{title}")
    expect(
      ok,
      `${DASHBOARD_HEADER}: the shared header's H1 must use ` +
        "`text-foreground`; every delegating page's title is rendered " +
        `here. Current H1 tag:\n  ${detail}`,
    ).toBe(true)
  })
})
