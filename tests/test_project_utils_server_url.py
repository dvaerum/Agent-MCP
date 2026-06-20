"""generate_system_prompt no longer reads MCP_SERVER_URL from the env.

The server URL in the worker connection snippet should be derived
from the running server's PORT (the same one the server bound to),
not pluggable via an env var that nothing else in the codebase reads.
Letting MCP_SERVER_URL override would let a stray export inject an
attacker-controlled URL into every agent's prompt.
"""

from __future__ import annotations

import pytest


def test_mcp_server_url_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "http://malicious.example/mcp")
    monkeypatch.setenv("PORT", "8080")

    from agent_mcp.utils.project_utils import generate_system_prompt

    prompt = generate_system_prompt(
        agent_id="worker-1",
        agent_token_for_prompt="tok-worker",
    )
    assert "malicious.example" not in prompt, (
        "MCP_SERVER_URL must not influence the connection snippet"
    )
    # And the fallback formula (PORT-derived /mcp URL) IS used.
    assert "http://localhost:8080/mcp" in prompt


def test_port_env_still_drives_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    monkeypatch.setenv("PORT", "9999")

    from agent_mcp.utils.project_utils import generate_system_prompt

    prompt = generate_system_prompt(
        agent_id="worker-1",
        agent_token_for_prompt="tok-worker",
    )
    assert "http://localhost:9999/mcp" in prompt
