# ADR-0006: Router does no MCP-protocol manipulation; it is a pure HTTP proxy + systemd lifecycle manager

## Status

**Extended by ADR-0020** (router is mount-agnostic), which carries the
same "the router doesn't manipulate MCP protocol semantics, it routes
bytes" principle further (mount/prefix derivation, root-aliased
routes) in the CoNexus Rust rewrite. Still the live architectural
stance for `conexus-router`.

**Provenance**: originally filed in the `home-manager-config` deploy
repo's `common/user/agent-mcp/docs/adr/` (as
`0006-router-does-no-mcp-manipulation.md`). Moved here 2026-09-06 —
see ADR-0003's own provenance note for why. The `prancy-napping-pie.md`
plan-file citation below is to a different, now-deleted ephemeral plan
file — see ADR-0005's own note on this. The cross-reference to "ADR
0001" below is to the DEPLOY REPO's own ADR-0001
(`fork-rather-than-patch-set`), which stayed in that repo (a
dependency-consumption strategy, not a code-architecture decision) —
it has no equivalent number here.

## Original decision (verbatim, unedited)

Through Phases 1–6, `router.py` accumulated MCP-protocol workarounds
for upstream gaps: synthetic tools injected into `tools/list`, schema
rewrites of upstream tool definitions, request-body injection to fill
in admin-only params, parallel admin sessions (`_mcp_call_admin`) to
silently promote worker calls, and an SSE inject queue splicing
synthetic events into the live stream. Phases 4–5 upstreamed the real
fixes into our fork (`dvaerum/Agent-MCP`), so these router-side
compensations now just drift from upstream and cause bugs: inconsistent
tool lists from router-side caching, "missing tool" debugging landing
in the router's filter logic instead of the tool's actual code, and
new MCP-protocol behavior having two possible homes.

Decision (`prancy-napping-pie.md` Q7.1, Phase 7f): the router becomes a
byte-level HTTP proxy for `/agent-mcp/__sse/*`, `/agent-mcp/__messages/*`,
and `/agent-mcp/__api/*`, plus the systemd lifecycle glue (`systemctl
--user start/stop agent-mcp@<name>`). It does not parse MCP frames,
does not know what a tool is, does not hold an admin session. Expected
size: ~2100 → ~800 LOC. The single-call ergonomics this costs (e.g.
assigning a task without first looking up the agent_id) move upstream
instead of disappearing — `assign_task` gets an `agent_id` parameter
in Phase 7d.

Hard rule going forward: new router-side synthetic tools are not
allowed. Any new agent-facing behavior lands upstream first, then the
router proxies it. Tool/protocol bugs are debugged in agent-mcp, not
in the deployer-owned router.

This extends ADR 0001 ("fix it at the source"): the source is now
upstream code, not router patches.
