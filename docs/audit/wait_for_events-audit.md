# Audit: existing `wait_for_events` infrastructure (2026-06-05, PR-2 prep)

Pre-implementation audit for the event-coord PR-2 work. This documents
the actual current behavior of every component the PR will touch so the
"before / after" delta is concrete.

## Files in scope

- `agent_mcp/tools/agent_communication_tools.py` (`wait_for_events_tool_impl`, lines 632-683).
- `agent_mcp/core/globals.py` (`signal_for`, `notify_agent_inbox`, lines 134-215).
- `agent_mcp/core/session_registry.py` (runtime queue fan-out — already wired).
- `agent_mcp/tools/task_tools.py` (`assign_task_tool_impl`, `_create_unassigned_tasks`).
- `agent_mcp/app/main_app.py` (`_patched_create_initialization_options`, alias warning pattern).
- `agent_mcp/prompts/__init__.py` + `catalog.json` (Prompt Book registration).
- `agent_mcp/tools/access.py` (`TOOL_ACCESS` — visibility classification).

## Behavior matrix vs. spec

| # | Spec requirement | Today | Delta needed |
|---|---|---|---|
| 1 | Default timeout 60s, configurable via `AGENT_MCP_EVENT_WAIT_TIMEOUT`, per-call override up to ceiling. | Default 60 (`WAIT_FOR_EVENTS_DEFAULT_TIMEOUT = 60`). Ceiling 900 (`WAIT_FOR_EVENTS_MAX_TIMEOUT = 900`). No env var. | Add env-var read at module load (or per-call). Lower ceiling to 300 per locked decisions table. |
| 2 | One-call-per-agent (HTTP-409-equivalent). | NOT enforced. A second concurrent call just clears the same `signal_for(agent_id)` Event and races. | Add `agent_event_locks: dict[str, asyncio.Lock]` in `globals.py`; tool acquires non-blocking; second call returns error envelope. |
| 3 | On every call, check `project_context.config_auto_event_loop_global` AND `agents.auto_event_loop`. If either OFF, return `stop_listening` immediately. | Not checked. | Add flag-check at top of impl; build `_stop_listening_envelope()` helper. |
| 4 | Mid-flight stop on flag flip — toggle-write code wakes affected waiters; on wake, rechecks flags. | No mid-flight wake path. Toggle dashboard endpoint doesn't notify. | Wire `signal_for(agent_id).set()` into the toggle-write paths (per-agent + global). After `wait()` returns, recheck flags before returning. |
| 5 | Hybrid event payload shape: `new_message`/`task_assigned` fat (existing `data:` blob), `unassigned_task_appeared` skinny (no description). | `new_message` shape uses `type="message"` + full row `data`. `task_assigned` shape uses full task row including `description`. No `unassigned_task_appeared` event type at all. `stop_listening` not present. | Add `unassigned_task_appeared` skinny event. Keep existing fat shapes for messages/assignments (spec-compliant). Add `stop_listening` event shape. |
| 6 | Capability subset routing for `unassigned_task_appeared`. | No code path. Unassigned task create does not wake anyone. | New helper `g.notify_unassigned_task_appeared(task_id, required_capabilities)` in `globals.py`; subset-match all non-terminated agents in Python; push event to per-agent queue + `signal_for(agent_id).set()`. Call site: `_create_unassigned_tasks`. Reuse `normalize_capabilities` helper from PR-1. |
| 7 | Per-event server-assigned cursor; `agents.last_event_seen_at` updated post-call. | `next_cursor` is the max event timestamp in the envelope. `agents.last_event_seen_at` is NOT written. | After returning events, UPDATE `agents.last_event_seen_at = max(timestamps)`. Keep existing `next_cursor` envelope field (it's already the API). |
| 8 | New tool `fetch_events_since(cursor)`. Pure DB query, no blocking. Uses `last_event_seen_at` if cursor is None. | Tool does not exist. Tool `wait_for_events` accepts a `since` parameter and the impl exposes a `_collect_events_for(agent_id, since)` helper that's exactly what this needs. | Add new tool `fetch_events_since`; thin wrapper around the existing collector + last-cursor lookup. Register in `TOOL_ACCESS` as `any`. |
| 9 | `serverInfo.instructions` wake-loop bootstrap, gated by both flags. | Alias-warning injection pattern present in `_patched_create_initialization_options`. No wake-loop text. | New module `agent_mcp/app/event_loop_instructions.py` holds `WAKE_LOOP_INSTRUCTIONS` constant. Extend the patched function to append the text when bearer's agent has both flags ON. |
| 10 | MCP prompt `agent-mcp-enter-event-loop` (same text). | Not registered in catalog. Prompt Book uses `catalog.json`. | Add entry to `catalog.json` with `category: coordination`. Template = `WAKE_LOOP_INSTRUCTIONS`. |
| 11 | New mutator hook for unassigned tasks. | `g.notify_agent_inbox(agent_id)` already wakes message + task-assigned. | `g.notify_unassigned_task_appeared(task_id, required_capabilities)` — additive. Call site: in `_create_unassigned_tasks`, after successful write. |

## Key existing primitives to REUSE (do not reinvent)

- `g.signal_for(agent_id)` — per-agent `asyncio.Event`, lazily created.
- `g.notify_agent_inbox(agent_id)` — already called by every mutator post-commit; sets signal + fans out `notifications/resources/updated` to GET /mcp streams.
- `_collect_events_for(agent_id, since)` (agent_communication_tools.py:490) — already returns chronologically-ordered events from `agent_messages` + `tasks`. New `fetch_events_since` becomes a thin wrapper.
- `_envelope(events, since)` — JSON serialization helper already in place.
- `_access._get_config_bool(key, default)` — reads `project_context[key]` as bool.
- `normalize_capabilities(caps)` — PR-1 helper for lowercase + dedupe.
- `request_alias_info` ContextVar pattern + `_patched_create_initialization_options` — exact mirror for the wake-loop injection.

## Existing tests (don't break)

- `tests/test_wait_for_events.py` — 9 passing tests; covers fast path, wake, broadcast, cursor advance, timeout clamp, per-agent scoping. None of them assert `unassigned_task_appeared` or stop_listening; the existing event-type strings are `message`, `broadcast`, `task_assigned`, `task_changed`.
- `tests/test_event_coord_schema.py` — PR-1's coverage; migration / schema invariants.

## Minimal-diff plan

The bulk of the long-poll plumbing is already shipping in production. PR-2's
core work is:

1. **Add** flag check + stop_listening envelope shape (gates).
2. **Add** per-agent serialization lock (concurrent-call rejection).
3. **Add** unassigned-task fanout helper + call site.
4. **Add** fetch_events_since tool (thin wrapper).
5. **Add** wake-loop instructions module + injection in initialize override.
6. **Add** MCP prompt entry in catalog.
7. **Update** cursor persistence to write `agents.last_event_seen_at`.
8. **Update** toggle-write code paths to wake waiters when flags flip.

All non-additive changes preserve the existing `wait_for_events` envelope
shape — `next_cursor` stays the same, existing event types
(`message`/`broadcast`/`task_assigned`/`task_changed`) keep their data
shape. New event types (`unassigned_task_appeared`, `stop_listening`) are
purely additive.
