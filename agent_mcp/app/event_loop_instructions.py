# Agent-MCP/agent_mcp/app/event_loop_instructions.py
"""Single source of truth for the wake-loop bootstrap instructions.

Two consumers read this constant verbatim:

1. ``_patched_create_initialization_options`` in ``main_app.py`` appends
   it to ``serverInfo.instructions`` on every ``initialize`` request
   where the calling bearer's agent has both global and per-agent
   ``auto_event_loop`` flags ON. This is the primary path — a freshly
   connected worker reads the instructions, calls
   ``fetch_events_since`` to drain its backlog, then enters the
   long-poll loop.

2. The MCP Prompt Book entry ``event-loop`` (registered
   via ``agent_mcp/prompts/catalog.json``) returns the same text as a
   prompt template. Operators / clients who want the wake-loop behavior
   without enabling the global toggle can invoke the prompt manually.

Keeping the text in one place means the two consumers can't drift —
which matters because the catalog is JSON (parsed once at import) and
the instructions are emitted on every initialize call.
"""

from __future__ import annotations


# IMPORTANT: This text is referenced by both the initialize-options
# injection and the MCP prompt fallback. Keep it agent-facing
# (imperative, second-person) — Claude Code / other LLM clients read
# this as authoritative guidance the same way they read tool
# descriptions.
WAKE_LOOP_INSTRUCTIONS = (
    "\n\n"
    "AGENT WAKE LOOP: On session start AND after recovery from any "
    "wait_for_events error: first call fetch_events_since(your_last_cursor) "
    "to drain anything missed since you were last connected. Then enter a "
    "loop where you call wait_for_events(); when it returns events, handle "
    "each, then call wait_for_events() again. "
    "EVENTS ARE POINTERS, NOT CONTENT — each one tells you something "
    "changed so YOU decide whether to go read it. A {type: \"message\"} or "
    "{type: \"broadcast\"} event carries the sender + subject/title + "
    "is_reply, NOT the body: call get_agent_messages to read the full "
    "message and act on it — that read is also what marks it read (an event "
    "you never open stays unread). A {type: \"task_assigned\"} or "
    "{type: \"task_changed\"} event carries the task title + status only: "
    "call view_tasks to read the description and work it. "
    "A {type: \"directive\"} event is the ONE exception that carries its "
    "content inline — its data.prompt IS a scheduled or ad-hoc command from "
    "your coordinator; do what it says now, then return to the loop. It is "
    "not a message from a peer; there is nothing to reply to. "
    "If you receive a {type: \"stop_listening\"} event, exit the loop and "
    "wait for human input.\n\n"
    "CRITICAL — wait_for_events() is your resting state, not a one-off "
    "check. The instant you finish ANY unit of work — a task marked "
    "complete, a message answered, a directive carried out — your very next "
    "action MUST be to call wait_for_events() again. Completing a task does "
    "NOT end your turn; returning to wait_for_events() does. You are 'done' "
    "only when a {type: \"stop_listening\"} event says so — until then, "
    "stopping means you go silent and miss every future task and message "
    "assigned to you. Never end your turn on a finished task; always end it "
    "parked in wait_for_events().\n\n"
    "FOREGROUND ONLY — call wait_for_events() directly and wait for it to "
    "return. Do NOT run it as a background task, a sub-agent, or a detached "
    "process. The loop works ONLY because the blocking call hands the event "
    "straight back into your active turn; a backgrounded poll delivers the "
    "event to a notification/output file you are not reading, so a message can "
    "arrive and you will never act on it.\n\n"
    "IMPORTANT — call wait_for_events() with NO arguments. Do NOT pass "
    "timeout_seconds (not even after an error). The server keeps your "
    "connection parked and alive with periodic heartbeats and returns ONLY "
    "when a real event actually arrives, so an idle loop costs you nothing. "
    "Passing timeout_seconds makes the call return an empty envelope on a "
    "timer, forcing a needless reconnect — and a wasted turn — every time it "
    "expires. When wait_for_events returns empty (a rare server-side "
    "recycle), just call it again immediately; do not add a timeout to "
    "'poll faster'."
)
