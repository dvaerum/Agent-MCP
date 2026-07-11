"""generate_system_prompt bakes no server URL into the worker prompt.

Historical context: the prompt used to embed a hand-rolled connection
snippet that computed a ``http://localhost:<PORT>/mcp`` URL. An earlier
hardening pass made sure that URL came from the server's own ``PORT``
(and NOT a pluggable ``MCP_SERVER_URL`` env var that nothing else reads),
so a stray export could not inject an attacker-controlled URL into every
agent's prompt.

arch-r3 #6a removed that connection snippet entirely: it re-taught a
FICTIONAL MCP protocol (``requests.post`` of a ``{"type": "tool_call"}``
body), which is not how MCP works — a spawned/registered worker talks to
the server through its real MCP client, which owns the wire. With the
snippet gone there is no server URL in the prompt at all, so the
env-injection surface is closed by construction. These tests pin that
neither an attacker-controlled ``MCP_SERVER_URL`` nor the PORT-derived
``/mcp`` URL leaks into the prompt.
"""

from __future__ import annotations

import pytest


def test_mcp_server_url_env_does_not_inject_into_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "http://malicious.example/mcp")
    monkeypatch.setenv("PORT", "8080")

    from agent_mcp.utils.project_utils import generate_system_prompt

    prompt = generate_system_prompt(
        agent_id="worker-1",
        agent_token_for_prompt="tok-worker",
    )
    assert "malicious.example" not in prompt, (
        "MCP_SERVER_URL must not influence the prompt"
    )


def test_prompt_bakes_no_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arch-r3 #6a: the connection snippet is gone, so no server URL —
    neither the PORT-derived ``/mcp`` endpoint nor any ``localhost`` URL —
    is embedded in the prompt. The MCP client already knows its endpoint."""
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    monkeypatch.setenv("PORT", "9999")

    from agent_mcp.utils.project_utils import generate_system_prompt

    prompt = generate_system_prompt(
        agent_id="worker-1",
        agent_token_for_prompt="tok-worker",
    )
    assert "http://localhost:9999/mcp" not in prompt
    assert "localhost" not in prompt
    assert "/mcp" not in prompt
