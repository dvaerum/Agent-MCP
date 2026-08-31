"""Regression-guard pins for the dashboard filter-bar labeling sweep.

Background. `messages-dashboard.tsx` was the one page that had already
solved a real problem: pick a value in a filter dropdown and the field
it belongs to disappears — "Assigned" or "High" sitting there with no
indication of which filter it came from. Messages fixed this with a
private `FilterField` helper (a small visible label stacked above each
control) alongside an `aria-label`/`ariaLabel` on the control itself —
but the fix was never promoted to `shared/`, so it stayed invisible to
every other list page.

The audit that followed found the SAME bug, independently regressed, on
four more pages:

  * Tasks — Status / Assignment / Created-by / Priority selects had
    neither a visible label nor an `aria-label` at all. Once picked,
    a value carries zero field context.
  * Agents — Status select, same gap.
  * Memories — the sort-order select, same gap (and it isn't even a
    filter — it's a sort, with nothing distinguishing it from one).
  * Prompt Book — Category select, same gap.
  * Schedules — had `aria-label`s already (the accessible half was
    fine) but no visible label (the sighted-user half wasn't).

`shared/filter-field.tsx` now holds the promoted `<FilterField>`. This
file pins that every filter/sort control in a list page's filter bar
carries BOTH halves of the fix — a nearby visible label (sighted users)
and an `aria-label`/`ariaLabel` (screen readers) — so a future filter
control can't ship half-labeled again.

Scope is deliberately the top-level list-page dashboards only (the
same `*-dashboard.tsx` set `_table_dashboards()` in
test_dashboard_polish_mobile_pass.py uses for its CC-7 audit) — NOT
every `<Select>` in every create/edit dialog across the app. A create-
dialog's own form fields are a different, already-adjacent concern
(most already use a real `<Label htmlFor>`, the standard shadcn form
idiom) and dialog-hosted tables/selects were deliberately scoped out of
CC-7 for the same reason: a different bar for a different UI role.

Some top-level dashboard files ALSO embed such a dialog inline (e.g.
prompt-book-dashboard.tsx's Prompt Builder, whose per-variable fields
pair `<Label htmlFor={variable.name}>` with `id={variable.name}` on the
control) — a real `for`/`id` association is the native HTML idiom and
strictly satisfies both halves of this audit on its own, so it's
recognized as a pass rather than special-cased out of the file glob.
Tests parse the .tsx source; no dashboard runtime needed (matches the
pattern set by test_dashboard_polish_mobile_pass.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("agent_mcp/dashboard")
COMPONENTS = ROOT / "components"
DASHBOARDS = COMPONENTS / "dashboard"


def _read(p: Path) -> str:
    return p.read_text()


# Comments legitimately mention `<SelectTrigger`/`<FilterField` when
# explaining this very audit (see this file's own docstring, and any
# in-source comment doing the same) — strip comments before scanning so
# a doc-comment can't count as a real control or a real label.
_JSX_COMMENT_RE = re.compile(r"\{/\*[\s\S]*?\*/\}")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", flags=re.MULTILINE)


def _code_only(src: str) -> str:
    src = _JSX_COMMENT_RE.sub("", src)
    src = _BLOCK_COMMENT_RE.sub("", src)
    src = _LINE_COMMENT_RE.sub("", src)
    return src


_TAG_START_RE = re.compile(r"<(SelectTrigger|AgentSelect)\b")


def _tag_span(src: str, start: int) -> str:
    """The JSX opening tag starting at `start` (the '<'), scanning to its
    OWN closing '>' while treating `{...}` JSX-expression braces as
    opaque. A prop like `onChange={(v) => f(v)}` contains a literal '>'
    from the arrow function that is NOT the tag's end — a naive
    `[^>]*>` regex stops there and silently truncates the tag, which is
    exactly the trap `<AgentSelect onChange={(v) => ...}>` sets.
    """
    depth = 0
    i = start
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return src[start : i + 1]
        i += 1
    return src[start:]


# A visible label nearby: either the promoted `<FilterField>` wrapper or
# a real shadcn `<Label>` (the form-field idiom used elsewhere — an
# equally-valid, pre-existing way to satisfy "visible label").
_VISIBLE_LABEL_RE = re.compile(r"<(FilterField|Label)\b")

# How far back from a control's own tag to look for its visible label.
# Generous enough to span a `<div className="relative">` wrapper (the
# search-input idiom) or a multi-line prop list, tight enough that it
# won't wander into an unrelated PRIOR control's label.
_LABEL_LOOKBACK = 400

# A control's `id=` attribute — either a string literal or a JSX
# expression — and a `<Label htmlFor=...>` using the same value is the
# native HTML label-association idiom (e.g. prompt-book-dashboard's
# per-variable builder fields: `<Label htmlFor={variable.name}>` paired
# with `<SelectTrigger id={variable.name}>`). It's STRICTLY better than
# either `aria-label` or a merely-nearby `<FilterField>`/`<Label>` — a
# real `for`/`id` pairing is what actually wires the two together for
# assistive tech — so a control with this pairing satisfies BOTH the
# accessible-name and visible-label requirements outright, regardless
# of distance (an unambiguous exact match, unlike an unlinked nearby
# label that could just be lucky proximity).
_ID_ATTR_RE = re.compile(r'\bid=(\{[^}]*\}|"[^"]*")')
_LABEL_FOR_RE = re.compile(r'<Label\b[^>]*?\bhtmlFor=(\{[^}]*\}|"[^"]*")', re.DOTALL)


def _has_matching_label_for(src: str, tag: str) -> bool:
    id_match = _ID_ATTR_RE.search(tag)
    if not id_match:
        return False
    control_id = id_match.group(1)
    return any(
        m.group(1) == control_id for m in _LABEL_FOR_RE.finditer(src)
    )


def _filter_bar_dashboards() -> list[Path]:
    """The list-page dashboards in scope for this audit — same
    top-level `*-dashboard.tsx` glob `_table_dashboards()` uses for
    CC-7, restricted to the ones that actually render a
    `<SelectTrigger>`/`<AgentSelect>` at all (a page with no dropdown
    filter has nothing for this audit to check).
    """
    found: list[Path] = []
    for f in sorted(DASHBOARDS.glob("*-dashboard.tsx")):
        src = _read(f)
        if "<SelectTrigger" in src or "<AgentSelect" in src:
            found.append(f)
    return found


def test_filter_and_sort_controls_are_self_describing() -> None:
    """Every `<SelectTrigger>` / `<AgentSelect>` in a list page's filter
    bar must have BOTH a nearby visible label (`<FilterField>` or
    `<Label>`) and an `aria-label`/`ariaLabel` on the control itself.
    Missing either one reproduces the exact bug found on
    Tasks/Agents/Memories/Prompt Book: a value that gives no indication
    of which filter it belongs to, for a sighted user (no visible
    label) or a screen reader user (no accessible name) respectively.
    """
    files = _filter_bar_dashboards()
    assert files, "filter-control audit set derived empty — the derivation is broken"

    failures: list[str] = []
    for f in files:
        src = _code_only(_read(f))
        for m in _TAG_START_RE.finditer(src):
            kind = m.group(1)
            tag = _tag_span(src, m.start())
            line = src[: m.start()].count("\n") + 1
            if _has_matching_label_for(src, tag):
                continue
            aria_attr = "aria-label=" if kind == "SelectTrigger" else "ariaLabel="
            has_aria = aria_attr in tag
            lookback_start = max(0, m.start() - _LABEL_LOOKBACK)
            has_visible_label = bool(
                _VISIBLE_LABEL_RE.search(src[lookback_start : m.start()])
            )
            if not has_aria:
                failures.append(
                    f"{f}:{line}  <{kind}> missing {aria_attr} "
                    "(no accessible name for screen readers)"
                )
            if not has_visible_label:
                failures.append(
                    f"{f}:{line}  <{kind}> has no nearby <FilterField> or "
                    "<Label> (no visible name for sighted users — a "
                    "picked value gives no indication of which filter "
                    "it belongs to)"
                )
    assert not failures, (
        "Filter/sort control(s) missing a visible label and/or "
        "aria-label (filter-bar labeling sweep):\n  " + "\n  ".join(failures)
    )
