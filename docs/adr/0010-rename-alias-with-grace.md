# ADR-0010: Project rename uses alias-with-grace; agent warning via `serverInfo.instructions`

## Status

Accepted (2026-06-03).

## Context

When an agent-mcp project is renamed, the old name is referenced by several
external artefacts:

- Claude Code MCP configs (`.mcp.json` files in worker checkouts)
- daemon-agent systemd unit names
- tokens files on disk
- URL bookmarks
- ad-hoc operator scripts

A hard rename breaks all of these silently. A registry-only alias (renaming
the logical name but leaving the workspace directory in place) is
non-disruptive at the request layer but leaves the workspace directory
lying about its identity, which confuses operators who `cd` into project
directories.

Workers using the old name need a way to discover the rename without
out-of-band notification — they can't be expected to read a Slack message
or check a dashboard.

A `mcp_sessions` telemetry table was introduced in PR #69 to track open
MCP sessions; renames need visibility into "is anyone still using the old
name?" to decide when it's safe to retire an alias.

## Decision

### Rename mechanics

Full rename: the workspace directory is moved on disk, the systemd unit is
renamed, tokens files are moved. Combined with a **configurable grace
period** (default 30 days) during which the OLD name resolves to the NEW
project via a registry-tracked alias.

Alias entries auto-expire via a periodic reaper. Operators can extend or
remove an alias manually via the dashboard.

### Agent warning

When a request arrives via an alias, the router injects an
`X-Agent-MCP-Alias: <alias_name>,<expires_at>` header into the backend
request. The backend appends a deprecation warning block to
`serverInfo.instructions` at MCP `initialize`. This is the
spec-standard surface that Claude Code reliably reads — it is the field
agent-mcp already uses for the system prompt, so we know it round-trips
intact.

Workers see the warning at the start of every session until their
`.mcp.json` is updated.

### Telemetry

Extend `mcp_sessions` table (PR #69) with an `alias_used TEXT NULL` column
plus an index on `(alias_used, last_seen_at)`. One row per opened MCP
session records whether it came in via an alias.

The dashboard surfaces alias usage as a chip on the renamed project's
card: "uses in last 7d / last-used-by / Extend / Remove" buttons.

## Consequences

- Renames are non-destructive: old configs keep working for 30 days, giving
  workers time to update on their own schedule.
- Operators have visibility into who's still using the old name and can
  make an informed decision before the alias expires.
- Agents get a clear deprecation signal in their system prompt at the
  start of every session — no spec extension required, no separate
  notification channel.
- Forever-aliases are prevented by the auto-expiring reaper. An operator
  who wants a permanent alias must affirmatively extend it.
- The router has a small amount of new state (alias table) and a small
  amount of new logic (alias resolution + header injection).
- The backend has a small amount of new logic (read header, append warning
  block to `serverInfo.instructions`).

## Alternatives considered

- **Registry-only alias** (α). Rename the logical name in the registry but
  leave the workspace directory in place. Rejected: the workspace directory
  name lies, confusing for operators who `cd` into project directories or
  list them on disk.
- **Hard rename without redirect** (β). Move everything; let the old name
  stop resolving immediately. Rejected: breaks all configs silently with no
  migration path. Workers see "connection refused" with no indication of
  what to update.
- **Custom MCP notification type for the warning.** Rejected: Claude Code
  currently drops unknown notification types (confirmed via background
  task #56 earlier in this design session). Using `serverInfo.instructions`
  is the only spec-standard surface known to reliably reach the worker.
- **Persistent alias with manual expiry only.** Rejected: aliases
  accumulate forever, becoming indistinguishable from real names over time.
  Auto-expiry with a configurable grace period prevents this.

## Links

- Plan: originally decisions #4 + #5 of the "prancy-napping-pie" working
  plan — an ephemeral Claude Code plan-mode file, never committed to
  this repo, no longer available. This ADR is the durable record.
- PR #85: Phase 1b rename + alias data model.
- PR #86: Phase 1c `serverInfo.instructions` injection +
  `mcp_sessions.alias_used` migration.
