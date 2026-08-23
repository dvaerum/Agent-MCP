# ADR-0009: Dashboard owns the ops surface; no separate `/__admin/`

## Status

Accepted (2026-06-03).

## Context

The router today serves an HTML index page at `/agent-mcp/` containing:

- a list of registered projects with status
- start/stop forms per project
- wiring snippets (Claude Code `.mcp.json` examples)
- the installer one-liner help

A new dashboard overview was scoped for multi-tenant deployments to give
operators a richer cross-project view. The natural question: do the router's
HTML index and the dashboard overview merge, or stay separate?

The router's HTML page is currently ~200 lines of f-string concatenation in
Python — it has already produced one NameError bug in production and is a
recurring source of HTML-escaping concerns.

## Decision

All ops functionality folds into the dashboard overview at
`/agent-mcp/__dashboard/`. The router's `/agent-mcp/` HTML page is deleted
and replaced by a redirect:

- multi-tenant: redirect to `/agent-mcp/__dashboard/`
- single-tenant: redirect to `/agent-mcp/__dashboard/<project>/`

One ops surface, audience-uniform. Both casual visitors and operators land
in the same React app.

## Consequences

- Wiring snippets, the installer one-liner, and the `.mcp.json` generator
  must be ported into the dashboard's React surface (Phase 3.5 scope; ~3–4
  days extra work).
- No more "which URL do I go to for X?" confusion — there is exactly one
  ops surface.
- Project list, status display, and start/stop forms naturally evolve into
  a richer UX (cards, search, filtering) instead of the static HTML page.
- The router's HTML rendering code (~200 LOC of f-string concatenation)
  gets deleted. The NameError bug class disappears with it.
- The router is reduced to a pure reverse proxy + UDS lifecycle manager.
  Smaller surface, fewer concerns, easier to test.

## Alternatives considered

- **Keep `/agent-mcp/` as a separate ops page.** Rejected: two-dashboards
  confusion, plus duplicate project-list rendering (one in Python f-strings,
  one in React).
- **Move ops to `/__admin/` URL.** Rejected: same problem as above, just
  renames the second surface. Operators still have to learn two URLs.
- **Delete ops affordances entirely.** Rejected: operators rely on the
  wiring snippets to set up Claude Code MCP configs. Without that
  affordance, every new project requires copy-pasting from documentation.

## Links

- Plan: originally decision #3 of the "prancy-napping-pie" working
  plan — an ephemeral Claude Code plan-mode file, never committed to
  this repo, no longer available. This ADR is the durable record.
- Phase 3.5 PR (when it ships) for the dashboard ops port.
