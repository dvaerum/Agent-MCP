# Duplication drift: a fact with no single home

Captured 2026-08-07, after a day in which the *same* root cause produced three
unrelated-looking failures in three different layers. Worth recording because
the symptom never looks like duplication — it looks like "the same bug keeps
coming back in different places."

## The pattern

When a fact is expressed in N places instead of one, the copies drift. Nothing
enforces agreement, so a fix applied to copy A never reaches copies B..N. The
observable symptom is a bug that *recurs* — in a different view, a different
job, a different release — and each recurrence gets fixed locally, which
guarantees the next one.

The remedy is always structural, never diligence: give the fact **one home** and
make the other sites *reference* it. A convention enforced socially (a comment
saying "keep in sync with X", a checklist, a code-review habit) is not a home.

## The three instances

### 1. Presentation logic duplicated per page (dashboard)

Every `*-dashboard.tsx` hand-rolled its own header, stats strip, loading
skeleton, empty state, error panel, forbidden panel, table, mobile list and
delete modal. The *infrastructure* layer underneath was already deep and correct
(one `request<T>()`, one 401 bounce, one 403 fold, abort-race guards), so the
problem was invisible in the data layer and lived entirely in presentation.

Evidence it was drift, not design: `StatsCard` existed in four copies that had
already diverged (different down-trend colour, one memoised and one not, `icon:
any` vs typed); two delete modals were 95 % identical files; one page had
reinvented its own `toast` — a same-name, different-implementation shadow of the
shared one, which it never imported. The git log showed the "parity standard"
being re-applied to one more page per PR (#505, #509, #513) — the same work,
paid three times.

Fix: a shared scaffold (`components/dashboard/shared/data-table-page.tsx` and
siblings) that owns the render precedence once. Their prop contracts are
documented in the component doc comments — that is the canonical reference, not
this file.

### 2. Dependency versions unbounded (CI)

An unpinned `ruff` drifted onto a release whose default rule set grew from 59 to
413 rules, and `tests/` went from clean to 1021 errors with no commit touching
it. The same class had also let `mcp` 2.0 in (1496 tests failing, latent behind
CI's short-circuit), plus a cryptography advisory and five HIGH npm advisories.

Three of the five failures were *invisible*: CI stops at the first failing step,
so fixing only the reported ones would have revealed a red pytest instead of a
green build.

Fix: pin the linter, cap the major, bump the vulnerable packages — and note that
a floating linter that can red the build on any upstream release is itself the
bug, independent of the violations it found.

### 3. Audit targets hardcoded (tests)

The dashboard parity audit enforced its standard by grepping each page and modal
from **hardcoded file lists**. Deleting a modal (legitimately, because it was
subsumed into a shared one) broke the audit, and a page that satisfied the
standard *by delegating to the shared scaffold* failed it, because the grep
looked only in the page file.

So the test encoded the very architecture we were dismantling: "every page must
re-implement this."

Fix: derive the target list (glob the tree) instead of hardcoding it, and make
the assertions delegation-aware — a page satisfies the standard directly *or* by
delegating to the shared component, and the shared component is asserted
directly so delegation is an equivalence, not an exemption. Widening the glob
immediately surfaced a real pre-existing bug the hardcoded list had been hiding.

## Rules of thumb this produced

- **A recurring bug in different places is a duplication report.** Ask "what
  fact lives in more than one place?" before fixing the instance.
- **Class-sweep every finding.** Fixing instances singly guarantees the class
  reappears; the sweep is what converges it.
- **Deletion test before extracting.** Imagine deleting the module: if
  complexity vanishes it was a pass-through; if it reappears across N callers it
  earned its keep. Do not split a file merely to reduce line count.
- **A test that pins markup pins an architecture.** When shared components
  legitimately absorb that markup, the test must become delegation-aware — and
  must then assert the shared component directly, or coverage silently weakens.
  Never edit an audit purely to make it pass; prove the negative still fails.
- **Deduplication must not change behaviour.** Adopting a shared component whose
  contract differs (e.g. a delete modal that gates on typing DELETE, applied to
  an action that was one-click) is a UX change wearing a refactor's clothes.
  Either keep the old behaviour or raise it as its own decision.
- **Silence is not health.** Several of these were invisible: behind a CI
  short-circuit, behind a vacuously-passing assertion, behind a log nobody
  routed anywhere. Prefer signals that fail loudly over ones that pass quietly.

## See also

- `docs/adr/0021-delivery-transport.md` — the delivery transport whose
  mode-inference bug (a session switched to ACP was still injected via the
  terminal route) was the day's fourth instance of the same shape: two places
  deciding "is this session ACP?" and only one of them updated.
- `components/dashboard/shared/*.tsx` — the scaffold contracts (canonical).
- `tests/test_dashboard_polish_mobile_pass.py` — the delegation-aware audit.
