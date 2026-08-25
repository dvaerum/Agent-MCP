# ADR 0026: `directive.due` — scheduled directives fire through the delivery-transport trigger set

**Status**: Accepted, 2026-08-25.
**Date**: 2026-08-25
**Builds on**: ADR-0011 (event-driven coordination / `wait_for_events`),
ADR-0021 (delivery transport — the per-worker SSE fallback push channel
and its background policy scheduler).

## Context

A live production session (`pikvm-manager`) was found sitting idle with a
scheduled directive (`sd_cb9e4377c87334b9`, 20-minute interval, "check in
with all workers") overdue by roughly three hours. Investigation traced
this to a structural gap: `collect_due_and_fire()`
(`agent_mcp/repositories/scheduled_directive_repository.py`) — the
function that evaluates and fires due scheduled directives — is **only
ever called from inside a live `wait_for_events`/`fetch_events_since` MCP
tool call** (`agent_mcp/tools/agent_communication_tools.py`,
`_collect_scheduled_directive_events_for`). A chat-style session that
never calls those tools (never runs its own polling loop) will never have
its directives evaluated, no matter how overdue they are. This is not a
bug in that specific session — it is the feature's design as shipped:
firing is entirely lazy and wait-loop-native.

Separately, ADR-0021 (three weeks **after** the scheduled-directive
feature landed) built exactly the missing piece for a different case
(unread messages / unfinished tasks / unassigned tasks): a per-worker SSE
"delivery transport" (`agent_mcp/features/delivery_transport.py`) that a
runtime (the AoE bridge plugin) subscribes to, plus a background
scheduler (`agent_mcp/features/delivery_scheduler.py`, `tick()`/
`run_loop()`) that proactively pushes skinny frames to any **connected**
worker whose policy condition is met — entirely independent of whether
that worker ever calls `wait_for_events`.

ADR-0021 explicitly designed its trigger set as extensible ("message.unread,
task.unfinished, task.unassigned, …") and states the policy loop was
"built on the existing reminder engine to avoid a parallel scheduler" —
i.e. the ADR's own intent is "add more triggers here," not "build a new
mechanism per trigger." Chronology confirms the original "no background
sweeper" choice in the scheduled-directive feature was correct *at the
time* it was made, simply because the connectivity registry + background
tick didn't exist yet — not because a sweeper was considered and rejected
on principle.

## Decision

Add `directive.due` as a new trigger on the existing delivery-transport
background scheduler, reusing 100% of the existing SSE transport,
connectivity registry, and background-loop wiring. **Zero changes to
`aoe-bridge/` (Rust)** — it is deliberately policy-blind (ADR-0021:
"agent-mcp owns policy, the runtime owns delivery") and already just
relays whatever frame arrives on the stream it subscribes to.

`tick()` gains a second, additive per-connected-agent step:
`_fire_due_directives()` calls the SAME `collect_due_and_fire()` the
wait-loop path already uses, and pushes each resulting event as a
`directive_due` delivery frame via the existing `delivery_transport.push`.

Config: `on_due_directives: bool` on `DeliveryPolicyConfig`, gated under
the existing master `config_delivery_enabled` switch, default `True`
(unlike `on_unassigned_tasks`, which defaults `False` because it is an
ambient/noisy signal — a due directive is an explicit, deliberate,
low-volume obligation an operator/agent configured on purpose; defaulting
it off would silently reproduce the exact bug this fixes).

## The semantic tension, and how it is resolved

`collect_due_and_fire` is **non-self-healing**: it mutates state
(`next_due_at = now + interval`, `run_count += 1`) unconditionally on
read, committing immediately. This differs from the other three delivery
triggers, which are self-healing (`evaluate_and_push` reads a live
backlog *count* each tick; nothing is consumed by a failed push, so it
simply re-evaluates true next tick — a frame that can't be delivered is
dropped and the policy re-fires next cycle). Naively calling
`collect_due_and_fire` then `push()` risks losing a fire if `push()`
returns `0` (no live subscriber / full queue).

**Resolution**: gate the new check on
`delivery_transport.connected_agent_ids()` — `tick()` already only
iterates connected agents, so a disconnected agent's directives are never
even read (preserving "offline fire → once on reconnect"). Within one
tick iteration, the whole check-then-fire-then-push sequence is plain
synchronous code with **no `await`** anywhere in the chain (`tick()` →
`_fire_due_directives()` → `collect_due_and_fire()` + `push()`). Python's
single-threaded asyncio cooperative model means no other coroutine — and
therefore no unsubscribe — can run between the connectivity check and the
push within one tick, closing the disconnect-race window entirely. The
only residual risk is the bounded per-stream queue (256 slots) being
genuinely full at push time — logged, and accepted as an extremely rare,
self-inflicted edge case, not a routine failure mode.

**Double-fire race between the two call sites** (`wait_for_events`'s
collector vs. the new tick): both run on the same single-threaded asyncio
event loop of the same per-project process (one systemd unit per
project), and neither awaits mid-transaction — so two calls for the same
agent can never interleave; whichever runs first fully commits before the
other is even scheduled, and `collect_due_and_fire`'s own
`WHERE next_due_at <= ?` re-evaluates freshly against the now-already-
advanced row. No new locking/claim needed for this single-process-per-
project deployment model — this is a documented scope boundary, not a new
risk; the property already existed implicitly with the one call site.

## Frame shape

`_render_directive_frame` wraps the SAME event shape
`collect_due_and_fire` already emits for the `wait_for_events` path
(`scheduled_directive_repository._directive_event`, unmodified), nested
under `directive`, so a downstream consumer sees identical directive
content regardless of which path delivered it:

```json
{"type": "delivery", "reason": "directive_due", "directive": {"type": "directive", "ref_id": "...", "timestamp": "...", "priority": "urgent", "data": {"prompt": "...", "source": "schedule", "schedule_id": "..."}}}
```

`reason="directive_due"` follows the existing snake_case reason
vocabulary (`unread_messages`/`unfinished_tasks`/`unassigned_tasks`).

**Documented asymmetry**: unlike the other three delivery triggers, this
frame is **not skinny-redacted**. A directive's `data.prompt` is
first-party content the agent/operator itself authored, not a third
party's message body — ADR-0021's "never ship bodies" concern (protecting
against secrets embedded in someone else's message text) doesn't apply
the same way to a schedule the agent's own operator configured.

## Consequences

**Positive**
- Closes the production gap with zero new infrastructure: no new
  transport, no new background loop, no new DB table — one more branch on
  an existing tick, reusing an existing firing function unmodified.
- No behavior change to the `wait_for_events`-native path:
  `collect_due_and_fire` and `_collect_scheduled_directive_events_for` are
  untouched; the new tick step is a second, unmodified caller.
- Zero `aoe-bridge/` (Rust) changes — confirmed by design (policy-blind
  relay, no branching on `reason` today).

**Negative / risks**
- `data.prompt` is not skinny-redacted like the other three delivery
  frames — an accepted, documented asymmetry (see above), not an
  oversight.
- One more DB round-trip per connected agent per tick
  (`TICK_INTERVAL_SECONDS` = 15s) when `on_due_directives` is armed —
  bounded by the number of connected delivery streams, not project size.
- **Scope boundary**: the double-fire safety argument assumes one process
  per project (this repo's current deployment model,
  `project_orchestrator.py`). A future horizontally-scaled deployment
  (multiple processes serving the same project) would need an atomic
  claim query (e.g. `UPDATE ... WHERE next_due_at <= ? RETURNING ...`)
  instead of the current SELECT-then-UPDATE, to avoid two processes both
  firing the same overdue directive.

## Verification

- `tests/test_delivery_scheduler_due_directives.py`: a connected-but-
  never-polling worker's overdue directive is fired by `tick()` (the RED
  case this ADR fixes); frame shape (`directive.data.prompt`,
  `source == "schedule"`); a not-yet-due directive is not pushed and
  `run_count` stays unchanged; a disconnected agent's directive row is
  never read by `tick()` (protects offline-fire-once-on-reconnect); the
  per-trigger `on_due_directives` toggle suppresses the new path; the
  master `config_delivery_enabled` switch short-circuits before the
  directive branch entirely.
- Existing suites (`test_delivery_scheduler.py`, `test_delivery_policy.py`,
  `test_scheduled_directive_firing.py`, `test_delivery_transport.py`,
  `test_settings_schema.py`) remain green — no behavior change to the
  paths they cover.
