# ADR-0003: Dashboard is admin by design; securing the URL is the deployer's job

## Status

**Superseded by ADR-0013** (operator login). Recorded here as history —
kept, not deleted, because ADR-0009 and ADR-0013 both assume/reference
this decision without ever stating it in full themselves.

**Provenance**: originally filed in the `home-manager-config` deploy
repo's `common/user/agent-mcp/docs/adr/` (as `0003-dashboard-admin-by-design.md`)
rather than here, back when this fork had no `docs/adr/` of its own.
Moved here 2026-09-06 since it records a decision about agent-mcp's
own authorization model, not a deployment choice — the deploy repo's
`docs/adr/0001`/`0002`/`0008` stayed put as genuinely deploy-specific
(dependency-consumption strategy, test-tier split, local-inference
backend wiring).

## Original decision (verbatim, unedited)

The dashboard has no login layer and never will at the agent-mcp
level. Anyone who can reach `/agent-mcp/__dashboard/<name>/` is
implicitly admin: they can fetch the admin token via `/api/tokens` and
use it for any write. This matches upstream's already-shipped model
(`/api/tokens` returns admin token to any caller; `prompt-book.ts`
treats the admin token as "the secret you copy") and avoids building
per-user authentication into agent-mcp, which would multiply scope.
Securing the dashboard URL is the deployer's responsibility —
reverse-proxy auth, Tailscale ACLs, IP allowlists, whatever fits.
Worker-tier escalation paths (issues I and O — `view_project_context`
and `/api/tokens` leaking admin token to worker bearers) are real bugs
and get fixed regardless; "dashboard = admin" only works if workers
actually stay workers.
