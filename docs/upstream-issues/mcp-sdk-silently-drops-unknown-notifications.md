# MCP SDK clients silently drop custom notification methods

Migrated 2026-09-06 from the deploy repo's (`home-manager-config`,
formerly `nixos-developer-system`) `docs/UPSTREAM_ISSUES.md` §R -- an
upstream MCP protocol/SDK tracking item (not an Agent-MCP bug), kept
because it's the direct rationale behind `wait_for_events`'s
long-polling design (ADR-0011/ADR-0012) still being the primary event
mechanism rather than a server-push notification.

## Observation

The MCP TypeScript SDK's `Client` only invokes handlers registered via
`setNotificationHandler(schema, handler)`; a notification whose
`method` isn't recognized is silently dropped. Same shape in the
Python SDK. Verified against Claude Code, which links the TS SDK.

## Impact for Agent-MCP

Any custom server→client notification Agent-MCP might want to push
(e.g. `notifications/agent_message`, `notifications/task_assigned`)
never reaches the consuming application over the notification channel
itself. The only working alternative is long-polling: `wait_for_events`
holds the request open until something arrives or the timeout fires.
That works, but the MCP spec permits custom notifications (JSON-RPC
§4.1 puts no constraint on `method`), so this is a client-side gap,
not a protocol limitation -- with a fallback hook, the long-poll path
becomes an optional compatibility fallback rather than the only
option.

## Filed upstream

- SDK proposal: [`modelcontextprotocol/typescript-sdk#2237`](https://github.com/modelcontextprotocol/typescript-sdk/issues/2237) — `Client.setFallbackNotificationHandler(handler)`.
- Spec discussion: [`modelcontextprotocol/modelcontextprotocol#2832`](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/2832) — a SHOULD-level conformance clause: "compliant clients SHOULD surface unknown notification types via a fallback hook, log, or UI affordance."

## What changes if/when the SDK ships the fallback API

`wait_for_events` stays useful (covers clients that haven't adopted
the fallback yet) but stops being the *only* path. Resources push and
`tools/list_changed` could then also reach non-current-request
sessions once a session-registry fan-out exists to route them —
tracked as a real design option, not committed to.
