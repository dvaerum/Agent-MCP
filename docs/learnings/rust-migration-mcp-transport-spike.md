# Phase 0 spike verdict: rmcp supports native server-push (no hand-rolled session/queue layer needed)

Context: the Python→Rust migration plan's Phase 0 called for a spike to
de-risk the single biggest external unknown before committing the
critical path — does the Rust MCP SDK support genuine server-initiated
push into a live session from a task unrelated to the request that
opened it? Python's `mcp` SDK does NOT support this in the stateless
mode this codebase runs in (`app/main_app.py` had to hand-roll `GET
/mcp` outside the SDK entirely, backed by a durable `mcp_sessions` DB
table + an in-memory per-session `asyncio.Queue` fan-out registry, to
get server-initiated wake-loop notifications working at all).

## What was tested

A minimal `rmcp` (3.1.4 — pinned to match the version this operator's
sibling repos `pikvm_mcp_server`/`m365-bridge` already use in
production) server exposing one tool, `register_and_wait`:

1. On call, it stores the request's `RequestContext::peer` — an
   `rmcp::Peer<RoleServer>` handle — in a registry keyed by an
   `agent_id` argument, then blocks (simulating `wait_for_events`'s
   hold) on a `oneshot::Receiver` up to a 10s timeout.
2. A completely separate `axum` route, `POST /trigger/:agent_id`, with
   no relationship whatsoever to the original tool-call request or its
   connection, looks up that `agent_id`'s stored `Peer` and calls
   `peer.send_notification(...)`.
3. If rmcp genuinely routes that notification onto session
   `agent_id`'s live SSE stream, the blocked tool call observes it and
   returns immediately — proving cross-task server push through rmcp's
   own session machinery, not a hand-rolled one.

Built on `rmcp::transport::streamable_http_server::tower::
StreamableHttpService` + `LocalSessionManager` (the same combination
`pikvm_mcp_server`'s real, already-shipping `http_server.rs` uses) and
`ServerHandler`'s `get_info`/`list_tools`/`call_tool` overrides (no
macro magic — matches the migration plan's own recommendation of
hand-written tool registration over `#[tool_router]`/`inventory`
auto-collection).

## Result: confirmed, live, end-to-end

```
$ curl -X POST http://127.0.0.1:18099/mcp ... "method":"initialize" ...
< mcp-session-id: 41f2a2f9-6194-4469-9b06-f0dbe87fc5b2

$ curl -X POST http://127.0.0.1:18099/mcp -H "Mcp-Session-Id: 41f2..." \
    -d '{"method":"tools/call","params":{"name":"register_and_wait", \
         "arguments":{"agent_id":"alice"}}}'
  # blocks — this connection is now parked, holding the live session

# a SECOND, unrelated curl process:
$ curl -X POST http://127.0.0.1:18099/trigger/alice
{"ok":true,"send_notification_result":"Ok(())"}

# the FIRST curl's blocked response returns immediately:
data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text",
  "text":"PUSH RECEIVED via rmcp Peer::send_notification: task_assigned
  notification pushed"}],"isError":false}}
```

The blocked `tools/call` returned in ~1s (not the 10s timeout),
triggered purely by the second, unrelated process's call — this is a
genuine, verified proof of cross-task server push via rmcp's native
`Peer`/`SessionManager` machinery.

## Implication for the migration plan

This is a real simplification versus what the plan's Phase 0 entry
hedged as a possible fallback ("port the `mcp_sessions`-table+queue
design as-is" if rmcp lacked this). It does not:

- rmcp's own `Peer<RoleServer>` handle **is** the fan-out mechanism —
  a plain `Arc<DashMap<AgentId, Peer<RoleServer>>>` registry (exactly
  the per-agent waiter registry shape the wake-loop design in Phase D3
  already called for) replaces the hand-rolled `mcp_sessions` DB table
  + `asyncio.Queue` pair entirely. No custom session-persistence layer
  needs to be built for the push-delivery half of the problem.
- `SessionManager` is a pluggable trait (`local::LocalSessionManager`
  used here; the trait's own docs describe backing it with "a
  database, Redis, or any other external store" and a documented
  `RestoreOutcome::Restored` path for exactly that) — if a future need
  arises for session state to survive a process restart (today's
  Python `mcp_sessions` table's actual reason to exist), that's a
  `SessionManager` implementation choice, not a from-scratch design.
- The actor-per-agent design (one `tokio::spawn`'d task per
  `agent_id`, `tokio::select!` over the 4-way deadline race) from the
  target architecture is UNCHANGED by this finding — what changes is
  only the mechanism an actor uses to deliver a notification to its
  connected client: `peer.send_notification(...)` instead of a
  hand-rolled SSE writer draining a queue.
- Still open, NOT resolved by this spike: whether `Peer::
  send_notification`'s delivery honors the exact "newest connection
  wins, old one gets `Superseded`" semantics when a SECOND `Peer` for
  the same `agent_id` opens before the first is done (this spike only
  proved single-waiter push; supersession needs its own targeted test
  in Phase D3, per the migration plan's own `loom`-based verification
  plan for that invariant).

## Practical notes for whoever builds the real thing

- rmcp 3.1.4, not 3.2.0: 3.2.0 changed several APIs used here
  (`CallToolResponse` return shape, deprecated the Logging
  notification family per SEP-2577, several structs went
  `#[non_exhaustive]` requiring builder methods instead of struct
  literals). 3.1.4 matches what `pikvm_mcp_server` and `m365-bridge`
  already ship, so staying on it avoids re-deriving API compatibility
  twice.
- `ProgressNotification` (not `LoggingMessageNotification`, which is
  deprecated by SEP-2577) is the right off-the-shelf notification type
  to prototype with; a real wake-loop port should define its own
  custom notification method rather than overload progress semantics.
- Construct `ServerInfo`/`Tool`/`ListToolsResult` via their builder
  methods (`ServerInfo::new(...).with_instructions(...)`, `Tool::new(
  name, description, schema)`, `ListToolsResult::with_all_items(...)`)
  — most model types are `#[non_exhaustive]` and reject direct struct-
  literal construction even with `..Default::default()`.

The spike code itself was not committed (it is genuinely throwaway,
per the migration plan's own framing) — this document is the durable
record of what was proven and how, so it doesn't need re-deriving.
