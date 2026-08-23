# Don't cite `~/.claude/plans/*.md` from committed docs

## What happened

Nine ADRs (`0008`, `0009`, `0010`, `0011`, `0013`, `0015`, `0016`,
`0017`, `0018`) cited `/home/dennis/.claude/plans/prancy-napping-pie.md`
as their "Plan:" source — the Claude Code plan-mode working document
for the router-upstream/operator-login/SSO effort (2026-06 through
2026-07). That file was never committed to this repo (plan-mode files
live only in `~/.claude/plans/`, local to one machine, outside git) and
had already stopped existing by 2026-08-23, when an unrelated session's
plan-mode invocation reused the same harness-generated slug and wrote
new, unrelated content to that same path.

No real information was lost — every citing ADR turned out to be fully
self-contained (Status/Context/Decision/Consequences/Alternatives all
present), and the "Plan:" line was a one-off provenance breadcrumb, not
something the ADR depended on. But the citation itself had already been
a dangling reference for weeks before anyone noticed, and the slug
collision made it point at actively misleading content instead of
nothing.

## The rule

A `~/.claude/plans/*.md` file is a **session-scoped, single-machine,
uncommitted scratchpad**. Nothing guarantees it survives:

- past the session that wrote it,
- a `git clone` on another machine,
- another *unrelated* plan-mode invocation reusing the same
  harness-assigned slug (slugs are not namespaced per topic — a fresh
  invocation can land on a filename a much older, unrelated plan already
  used).

Never cite one from a committed doc (ADR, `docs/proposals/`,
`docs/learnings/`, code comments) as if it were a durable reference.
If a decision from a planning session matters long-term, promote it
into the committed doc itself — an ADR's `Context`/`Decision` sections,
a `docs/proposals/*.md` file, or a code comment that restates the
rationale inline — and treat the plan file as scratch that doesn't need
to survive.

If you want to name-drop a working-session codename for historical
color (e.g. "Phase 3 Wave 3 of the router-upstream plan"), that's fine
— it costs nothing and helps correlate commits from the same era. Just
don't imply the file backing that name is retrievable.
