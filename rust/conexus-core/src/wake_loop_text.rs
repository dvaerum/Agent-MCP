//! `WAKE_LOOP_INSTRUCTIONS` — shared verbatim text needed by TWO
//! independent call sites once Phase E1 PR B1 wires the prompt-book
//! `event-loop` entry: `conexus-backend`'s `initialize.instructions`
//! contributor (added in PR A) and `conexus-tools`'s prompt catalogue
//! (a sibling of `conexus-backend`, not a dependent of it — the
//! constant can't live in `conexus-backend` without an upward
//! dependency the workspace's layering forbids). `conexus-core` is
//! the natural shared home: zero I/O, no shared mutable state, sits
//! below both consumers.
//!
//! Port of `agent_mcp/app/event_loop_instructions.py::
//! WAKE_LOOP_INSTRUCTIONS`. Python keeps the two consumers
//! (`_patched_create_initialization_options`'s injection and the
//! `event-loop` prompt-book entry) in sync by having BOTH read this
//! one module-level constant rather than each carrying its own copy —
//! this promotion preserves that same "one constant, two readers"
//! contract in Rust.

/// ADR-0011 / event-coord wake-loop bootstrap text. Verbatim port —
/// MCP clients (and the `event-loop` prompt) read this as
/// authoritative agent-facing guidance, the same way they read tool
/// descriptions.
pub const WAKE_LOOP_INSTRUCTIONS: &str = "\n\n\
AGENT WAKE LOOP: On session start AND after recovery from any \
wait_for_events error: first call fetch_events_since(your_last_cursor) \
to drain anything missed since you were last connected. Then enter a \
loop where you call wait_for_events(); when it returns events, handle \
each, then call wait_for_events() again. \
EVENTS ARE POINTERS, NOT CONTENT — each one tells you something \
changed so YOU decide whether to go read it. A {type: \"message\"} or \
{type: \"broadcast\"} event carries the sender + subject/title + \
is_reply, NOT the body: call get_agent_messages to read the full \
message and act on it — that read is also what marks it read (an event \
you never open stays unread). A {type: \"task_assigned\"} or \
{type: \"task_changed\"} event carries the task title + status only: \
call view_tasks to read the description and work it. \
A {type: \"directive\"} event is the ONE exception that carries its \
content inline — its data.prompt IS a scheduled or ad-hoc command from \
your coordinator; do what it says now, then return to the loop. It is \
not a message from a peer; there is nothing to reply to. \
If you receive a {type: \"stop_listening\"} event, exit the loop and \
wait for human input. \
If you receive a {type: \"connection_superseded\"} event, a newer \
wait_for_events connection for you replaced this one — do NOT exit the \
loop and do NOT open another connection; you already have exactly one \
live connection carrying the loop. Keep exactly ONE wait_for_events \
call in flight at a time.\n\n\
CRITICAL — wait_for_events() is your resting state, not a one-off \
check. The instant you finish ANY unit of work — a task marked \
complete, a message answered, a directive carried out — your very next \
action MUST be to call wait_for_events() again. Completing a task does \
NOT end your turn; returning to wait_for_events() does. You are 'done' \
only when a {type: \"stop_listening\"} event says so — until then, \
stopping means you go silent and miss every future task and message \
assigned to you. Never end your turn on a finished task; always end it \
parked in wait_for_events().\n\n\
FOREGROUND ONLY — call wait_for_events() directly and wait for it to \
return. Do NOT run it as a background task, a sub-agent, or a detached \
process. The loop works ONLY because the blocking call hands the event \
straight back into your active turn; a backgrounded poll delivers the \
event to a notification/output file you are not reading, so a message can \
arrive and you will never act on it.\n\n\
IMPORTANT — call wait_for_events() with NO arguments. Do NOT pass \
timeout_seconds (not even after an error). The server keeps your \
connection parked and alive with periodic heartbeats and returns ONLY \
when a real event actually arrives, so an idle loop costs you nothing. \
Passing timeout_seconds makes the call return an empty envelope on a \
timer, forcing a needless reconnect — and a wasted turn — every time it \
expires. When wait_for_events returns empty (a rare server-side \
recycle), just call it again immediately; do not add a timeout to \
'poll faster'.";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_every_event_type_a_worker_must_handle() {
        // Regression guard for a byte-for-byte port: if this drifts
        // from Python's real text, a client relying on the documented
        // event vocabulary silently loses guidance.
        for needle in [
            "wait_for_events",
            "fetch_events_since",
            "stop_listening",
            "connection_superseded",
            "task_assigned",
            "task_changed",
            "directive",
        ] {
            assert!(
                WAKE_LOOP_INSTRUCTIONS.contains(needle),
                "missing expected fragment: {needle}"
            );
        }
    }
}
