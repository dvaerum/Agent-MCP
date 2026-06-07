# ADR-0012: `wait_for_events` fan-out — reverse the "one consumer per agent" lock

## Status

Accepted (2026-06-07). Supersedes the "Per-agent serialization lock"
row of ADR-0011 (PR #128 hardening).

## Context

ADR-0011 shipped `wait_for_events` with a per-agent `asyncio.Lock`
enforcing "only one in-flight long-poll per agent" (PR #128). A
second concurrent call from the same bearer returned an HTTP-409
analog envelope:

```json
{"error": "another_wait_in_flight", "agent_id": "alice",
 "message": "Another wait_for_events call is already in flight..."}
```

The framing was "a single agent should not have two consumers of the
same event stream — that's a bug, surface it." In practice the
v5.0.21 production deployment exposed a legitimate dual-use case
that the original design did not anticipate:

A `backend-dev` agent runs as a long-lived Claude Code MCP session
which holds the wait_for_events call to drive its event loop. A
human operator (Dennis) ALSO wants to curl `wait_for_events` from a
shell with the same bearer — both as an observability probe (was a
message just delivered?) and as a Path-B verification surface for
new PRs (two parallel shells, one notify, both must return). With
the 409 lock the second shell got the conflict envelope and the
human had no way to see the stream without killing the worker's
session.

The reporter's framing in the grilling: *"no one should use 409 as a
feature, we do not need to worry about that."* The lock was solving
a problem that didn't exist.

## Decision

| Decision | Choice |
|--|--|
| Concurrent waiters per agent | **N supported**. Every call to `wait_for_events` gets its own `asyncio.Queue`; the EventBus walks every queue under the agent on notify. |
| Wake mechanism | Per-call `await waiter_queue.get()` with a slice timeout. The shared `signal_for(agent_id)` `asyncio.Event` is kept as a wake-edge for any third-party that still `await`s it directly, but the canonical wake is now the per-waiter queue. |
| Synthetic events | Pushed onto every registered waiter's queue (`state.dispatch_synthetic_event`). Pre-fan-out a single shared queue was drained destructively by the first waiter; now each waiter sees every event. |
| DB-backed events | Notifier puts a `WAITER_WAKE_SENTINEL` on each queue to release the `get()`; the waiter then re-queries SQLite. Each waiter does its own SELECT — no shared state, no drain race. |
| Dashboard `wait_for_events_in_flight` | Computed from `state.waiter_count(agent_id) > 0` instead of `lock.locked()`. Semantics widen from "this single call is in flight" to "≥1 call is in flight". The dashboard chip + Settings page count consume the same boolean shape. |
| 409 envelope | **Retired.** The error shape is gone; second concurrent callers park normally and share the wake. |
| Legacy exports (`lock_for`, `agent_event_locks`, `drain_events`, `push_event`) | Kept as no-op / forwarder shims so any third-party import keeps resolving without break. Nothing inside the codebase relies on them anymore. |

## Why fan-out has no correctness cost

The 409 lock was framed as "one consumer per agent." The framing was
misleading: `wait_for_events` is a **notification** stream, not a
work-ticket queue. The events it carries are:

* `new_message`, `broadcast` — read from `agent_messages` via SELECT.
  The DB row is committed BEFORE the notify fires. Two readers
  hitting the same SELECT both see the row; there's no
  "deliver-once" semantic the lock was protecting.
* `task_assigned`, `task_changed` — same: rows in `tasks` /
  `task_assignments`, durable, idempotent SELECT.
* `unassigned_task_appeared` — synthetic (no per-recipient DB row).
  Pre-fan-out this rode on a shared per-agent queue and was drained
  destructively. Post-fan-out each waiter has its own queue; both
  observe the event.

In every case the event is an **observation** of a state change
that's already durable in SQLite. Multiple observers observing the
same state is correct by construction.

## Alternatives considered (and rejected)

| Alternative | Why rejected |
|--|--|
| Add `reject_concurrent: bool` arg to `wait_for_events` | Dennis: *"no one should use 409 as a feature."* Backward-compat shims for an anti-spec just preserve the bug surface. |
| Counting semaphore that allows up to N waiters | Same shape as the lock with a tunable knob; doesn't solve the underlying problem (the dual-use case wants UNLIMITED concurrent waiters, not bounded). |
| Single shared queue with broadcast semantics | `asyncio.Queue` is single-consumer by design; would need a custom broadcast type. Per-call queues are simpler and the GC story is trivial (registry de-list on exit). |
| Keep the lock; document the limitation | The limitation was the bug. The whole point of the PR is to remove the dual-use friction. |

## Migration cost

* `tests/test_event_coord_serialization.py` — **deleted**. Its core
  assertion (second concurrent call returns 409) is now anti-spec.
* `tests/test_dashboard_wait_in_flight_flag.py` — migrated from
  `g.lock_for("alice").acquire()` to `g.register_waiter("alice")`.
  Plus a new assertion that two concurrent waiters both surface
  TRUE.
* New regression suite `tests/test_wait_for_events_fanout.py`
  pins the fan-out semantics (5 tests covering DB-backed events,
  synthetic events, single-waiter regression, and timeout behavior).
* No dashboard frontend change — `wait_for_events_in_flight` keeps
  its boolean shape.
* No worker-side change — clients that issue a single
  `wait_for_events` call see identical behavior.

## Reusable utilities

| Symbol | Purpose |
|--|--|
| `state.register_waiter(agent_id) -> asyncio.Queue` | Allocate + register the per-call queue. Called on entry to `wait_for_events_tool_impl`. |
| `state.unregister_waiter(agent_id, queue)` | Idempotent cleanup. Called in the `finally` block. |
| `state.waiter_count(agent_id) -> int` | Snapshot for the `/api/all-data` dashboard surface. |
| `state.notify_waiters(agent_id)` | Push a wake sentinel onto every waiter queue. Used by `EventBus.LongPollSignalAdapter` for DB-backed event types. |
| `state.dispatch_synthetic_event(agent_id, event)` | Fan-out a synthetic event (no DB row) onto every waiter queue. Used for `unassigned_task_appeared`. |
| `state.drain_waiter_queue(queue)` | Non-blocking drain that filters out wake sentinels. Used by `wait_for_events_tool_impl` on every wake. |

## Operational notes

* The retired `agent_event_locks` dict still exists in
  `agent_mcp/core/state.py` — empty by default, lazily populated by
  any third-party that still calls `lock_for(agent_id)`. Slated for
  full removal in a future cleanup once we've audited there are no
  remaining external consumers.
* The per-agent shared `asyncio.Event` (`signal_for(agent_id)`)
  is still set by every notify — kept so anything that `await`s it
  directly (the only known caller is the toggle-flip wake path,
  which now also calls `notify_waiters`) keeps working.
* Per-call queue lifecycle: `register_waiter` lazily creates the
  per-agent list; `unregister_waiter` drops the list when empty so
  the registry doesn't accumulate empty entries for agents that
  briefly waited and exited.

## References

* PR #128 (event-coord PR-2) — the lock this ADR retires.
* PR #136 (Wave-2b EventBus) — the adapter pattern fan-out routes
  through.
* ADR-0011 — the parent decision record for the event-coord system.
* `tests/test_wait_for_events_fanout.py` — the new regression suite.
