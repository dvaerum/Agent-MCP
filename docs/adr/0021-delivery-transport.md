# ADR 0021: Delivery transport — an out-of-band fallback push channel for agents that don't poll

**Status**: Accepted (design), 2026-08-05. Supersedes the `aoe_notify`
push (`features/aoe_notify.py` + the `config_aoe_*` settings), which is
retired once this lands and is proven.
**Date**: 2026-08-05
**Builds on**: ADR-0011 (event-driven coordination / `wait_for_events`),
ADR-0012 (`wait_for_events` fan-out), ADR-0016 (config in `project_settings`),
ADR-0018 (settings-schema registry).

## Context

Agents coordinate by **pulling** events — a Claude Code session calls
`wait_for_events` and drains its inbox/tasks. ADR-0011 deliberately made
MCP push *not* wake an idle session, and named an external nudge channel
(AoE's `aoe_notify`) as the redundancy for that gap.

Two things forced a redesign:

1. **Some sessions never pull correctly.** In practice a number of Claude
   sessions stop calling `wait_for_events` (or never start), so they
   silently miss messages and task assignments. `wait_for_events` is also
   **newest-wins** per agent (`agent_communication_tools.py:2205` supersedes
   prior waiters), so a helper cannot simply hold the poll on the session's
   behalf without fighting the session's own calls.
2. **`aoe_notify` is the wrong shape.** It is an *outbound* HTTP push from
   agent-mcp **to** AoE (`POST {aoe}/api/sessions/<id>/send`), off by
   default, messages-only, and it couples agent-mcp to AoE's REST surface +
   a title→session-id heuristic. The dependency points the wrong way: the
   orchestrator (AoE) should reach *into* agent-mcp, not the reverse.

We want a channel where agent-mcp can get a notification **into a session
that isn't polling**, driven by a tunable policy, reaching the session
through whatever runtime owns it — without agent-mcp knowing anything about
that runtime.

## Decision

**A per-worker "delivery transport": a standard, documented channel a
runtime opens *to* agent-mcp, over which agent-mcp pushes skinny
notifications and the runtime reports session status.** agent-mcp owns the
*policy* (when to fire); the runtime owns *delivery* (getting text into the
session). agent-mcp stays ignorant of the runtime.

The first runtime is an **AoE plugin** (a persistent daemon holding one
connection per session, injecting via AoE's `/send` (tmux) and
`/acp/prompt` (structured) routes). Because the API is standard and keyed
by `(endpoint, token)` per session, a session's fallback can point at any
agent-mcp server or any compatible implementer.

### The channel (per worker, token-authed)

Authenticated by the **worker's existing bearer token** — the same token
its MCP tools use (one token, both purposes; the runtime already holds it
because it wired the per-session MCP entry). Endpoints, under the project
mount:

- `GET  /api/<project>/delivery/stream` — **SSE down.** agent-mcp streams
  notification frames the instant they're produced. One stream per worker.
- `POST /api/<project>/delivery/status` — **status up.** The runtime
  reports the worker's session `transport-status` ∈
  `{working, idle, dormant, dead}`.
- Deregistration is implicit: `status=dead` + stream close on session end;
  a transient stream drop is *not* an end (registration is kept, policy
  re-evaluated on reconnect).

**Transport-status is a SEPARATE presence field.** agent-mcp keeps its own
connection-presence (parked-waiter / live-stream derived) untouched, and
exposes the runtime-reported status alongside it. Readers choose; the
fallback policy reads the runtime status (it's the accurate "is it busy"
signal for a non-polling session), existing consumers are unaffected.

### What is pushed (content-only, skinny by default)

Frames carry **content, not a bare pointer** — but **skinny by default**:
exactly the fields the event loop already exposes (message/task **id,
title/subject, status, priority, sender**), **never the full body**. Bodies
(and any secrets in them) stay out of the pane; the agent pulls the body
via its MCP tools if it wants. agent-mcp renders each frame via a
per-project template; the template may be widened to include more only by
explicit opt-in. Delivery is **immediate** (the session buffers it); the
status field does not gate delivery.

### When it fires (tunable per-project policy)

agent-mcp owns a per-project **fallback policy** (a `project_settings`
group, ADR-0016/0018), built on the existing idle-backlog reminder engine:

- **Triggers** (extensible on/off set): `message.unread`,
  `task.unfinished`, `task.unassigned`, …
- **Cadence**: **escalating backoff** while a condition stays unmet (ping
  soon, then widening gaps to a cap), **reset when the condition clears**.
- **Status cooldown**: suppress while `transport-status == working`; a
  "wake dormant?" knob decides whether a ping may wake a `dormant` session;
  a cooldown window after each ping.
- **No delivered-state, no ack.** The *condition* is the source of truth —
  a ping never marks a message read or a task done. The agent acting is
  what clears the condition and stops the pings; a ping that didn't land
  (runtime down) simply re-fires next cycle. Self-healing by re-evaluation.

### Identity lifecycle

On session **end** the runtime posts `status=dead` and deregisters; the
agent-mcp **agent row + token persist** — the inbox/history survive and a
restarted session re-attaches as the *same* identity (these agents are
long-lived and address each other by name). A **transient** stream drop is
treated as temporarily-gone, not ended.

### Per-session MCP (the agent's tools) — an AoE-side capability

Orthogonal but part of the same story: giving each session its own
agent-mcp identity/token as a first-class MCP tool needs **per-session MCP
config**, which AoE lacks (its MCP config is per-agent/profile/project, never
per-session). That is an **AoE patch** (general per-session MCP servers —
`http {url, headers}` / `stdio` / `sse`; settable at create and on a running
session via respawn-on-next-idle; a `session.mcp` capability, trust-by-grant;
targets any session). It is not an agent-mcp change; agent-mcp is merely the
first consumer. The **same token** wired into the session's MCP entry is the
token the delivery transport authenticates with — one token links tools,
fallback, and identity.

## Consequences

**Positive**
- Sessions that never poll still receive their messages/tasks, on a tunable
  schedule — the core failure this addresses.
- The dependency direction flips: the runtime reaches *into* agent-mcp;
  agent-mcp needs no knowledge of AoE (no REST calls, no title heuristic).
- One standard, portable API — a session can point its fallback at any
  compatible server; agent-mcp gains real per-worker status without
  disturbing its existing presence.
- Retires `aoe_notify` + `config_aoe_*` — one mechanism, less coupling.
- Skinny-by-default keeps bodies/secrets out of panes (aligns with ADR-0017's
  "protect by authorization, not by guessing content" — here, by not shipping
  the body at all).

**Negative / risks**
- A new always-open SSE per worker (N streams from one runtime) — bounded by
  the same stream infra the MCP wire already uses; the idle-stop router must
  keep the delivery streams alive like MCP streams.
- The policy is a new stateful loop (backoff timers, per-worker cooldown) —
  built on the existing reminder engine to avoid a parallel scheduler.
- Content in the pane is opt-in beyond skinny; the default must stay skinny
  or the privacy property regresses.
- Trusting a runtime-reported status is a new input; it only *gates* the
  fallback (never authorizes anything), so the blast radius is "nudge timing".

## Verification

- **Policy engine**: unit tests for each trigger's arm/clear, escalating
  backoff (widen + reset-on-clear), status cooldown (suppress while
  `working`, wake-dormant knob), and self-healing (missed ping re-fires;
  never mutates read/done state).
- **Channel**: worker-bearer auth on `/delivery/stream` + `/delivery/status`;
  a frame produced by a `send_agent_message` reaches a connected stream;
  skinny-by-default rendering (id/title/status, no body).
- **Presence**: transport-status is a distinct field; agent-mcp's own
  connection-presence is unchanged for existing consumers.
- **Lifecycle**: `status=dead` deregisters but the agent row + token persist
  (a re-attach resumes the same identity); a transient drop keeps the
  registration and re-evaluates on reconnect.
- **Migration**: with the delivery path enabled, `aoe_notify` is disabled
  and then removed; no `config_aoe_*` reader remains.
