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

2. The MCP Prompt Book entry ``agent-mcp-enter-event-loop`` (registered
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
    "to drain anything missed since you were last connected. Handle each "
    "returned event (call view_messages or view_task to fetch full content "
    "for skinny events). Then enter a loop where you call wait_for_events(); "
    "when it returns events, handle each, then call wait_for_events() again. "
    "If you receive a {type: \"stop_listening\"} event, exit the loop and "
    "wait for human input. This is your default idle behavior; do this "
    "whenever you have no other work in progress."
)
