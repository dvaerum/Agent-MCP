"""arch-r3 #6a — the worker system prompt must not bake a fictional MCP protocol.

Bug: ``generate_system_prompt`` used to embed a ~40-line hand-rolled
``call_mcp_tool`` Python snippet that told the agent it was "running in
Cursor" and to ``requests.post`` JSON ``{"type": "tool_call", ...}`` to a
``/mcp`` endpoint. That is NOT how MCP works — a spawned/registered worker
talks to the server through the real MCP client, which owns the wire
protocol. So every worker received WRONG tool-calling instructions in its
live system prompt.

Fix (best-long-term, locked in the plan): drop the fictional snippet and
the "running in Cursor" framing; the system prompt should not re-teach a
(wrong) protocol. These tests pin that the fictional strings are gone AND
that a usable worker prompt still renders (role label + goal).
"""

from __future__ import annotations

from agent_mcp.utils.project_utils import generate_system_prompt


# Substrings that only appear in the fictional hand-rolled protocol.
_FICTIONAL_SUBSTRINGS = [
    '"type": "tool_call"',
    "running in Cursor",
    "call_mcp_tool",
    "requests.post",
]


def test_prompt_omits_fictional_mcp_protocol() -> None:
    prompt = generate_system_prompt(
        agent_id="worker-6a",
        agent_token_for_prompt="tok-worker-6a",
    )
    for needle in _FICTIONAL_SUBSTRINGS:
        assert needle not in prompt, (
            f"system prompt must not contain the fictional MCP protocol "
            f"string {needle!r}"
        )


def test_prompt_still_renders_usable_worker_prompt() -> None:
    prompt = generate_system_prompt(
        agent_id="worker-6a",
        agent_token_for_prompt="tok-worker-6a",
    )
    # Role label (agent_role-derived) is preserved.
    assert "Worker" in prompt, "worker role label must be present"
    assert "worker-6a" in prompt, "agent id must be present"
    # The goal / core-responsibilities framing is preserved.
    assert "Your goal is to complete tasks" in prompt
    # Still an MCP prompt — just without the invented wire protocol.
    assert "MCP" in prompt
