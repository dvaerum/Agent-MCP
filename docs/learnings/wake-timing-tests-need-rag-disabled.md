# Wake/event-timing tests must disable RAG placement analysis

Symptom: `assign_task` wake-event tests (`test_wake_on_task_assigned`,
`test_assign_task_to_offline_agent_returns_promptly`,
`test_task_assigned_event_is_skinny`) fail intermittently — usually
"want 1 event; got: {'events': []}" or a `_BOUND_SECONDS`/`wait_for`
timeout — with no code change. They pass reliably in normal full-suite
CI but fail deterministically on a busy dev host, or when run alongside
other RAG-touching work.

Root cause: `assign_task_tool_impl`'s create-and-assign path
unconditionally runs `validate_task_placement` (a real RAG call —
embedding + LLM completion) unless `ENABLE_TASK_PLACEMENT_RAG` is
disabled. These tests assert wake-event *timing* under a tight budget
(3–5s), but that budget has to absorb the RAG call's real latency too,
since the task isn't actually created (and the wake event isn't
published) until placement analysis returns. On a host whose RAG
backend serializes requests (this dev box's Ollama runs `-np 1`, so any
concurrent RAG-touching session or test contends for the one worker
slot), that latency can spike well past 5 seconds — a false failure
about wake-event *delivery* that's actually about RAG-call *queueing*.

Fix: `monkeypatch.setattr("agent_mcp.tools.task_tools.ENABLE_TASK_PLACEMENT_RAG", False)`
before calling `assign_task_tool_impl` (or the `assign_task` MCP tool)
in any test whose assertion is about timing, not placement-analysis
content. `tests/test_sec_r3_task_cache.py` already used this pattern;
the 3 wake-timing tests above just hadn't adopted it. Apply the same
monkeypatch to any *new* test that times an `assign_task` call.
