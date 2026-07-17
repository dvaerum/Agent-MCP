"""Token-retirement PR 1 (Phase A) — ``get_system_prompt`` migration.

The token-retirement plan (``prancy-napping-pie.md`` → "Retire the
legacy ``token`` auth parameter") Phase A migrates the tools that
structurally read ``arguments["token"]`` to consume the threaded
:class:`Principal` instead. ``get_system_prompt`` was the ONLY tool
with a real identity dependency on the token arg (it fed
``get_agent_id(arguments["token"])`` and embedded the same bearer in
the generated connection snippet).

This test pins the migration: called with a Principal and NO ``token``
in ``arguments``, the tool

  * derives the requesting agent id from ``principal.agent_id``, and
  * feeds ``principal.source_token`` to the connection-snippet builder,

WITHOUT ever consulting a ``token`` argument. This is the proof the
tool derives identity solely from the Principal.

Phase C (token-retirement PR 3) removed the ``get_agent_id`` fallback
import from ``agent_tools`` entirely and dropped the ``token`` property
from the schema, so the old "``get_agent_id`` must not be called" guard
is now structurally guaranteed and no longer stubbed here.
"""
from __future__ import annotations

import pytest

import agent_mcp.tools.agent_tools as agent_tools_module
from tests.harness import make_principal

pytestmark = pytest.mark.asyncio


async def test_get_system_prompt_derives_identity_from_principal(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_generate(*, agent_id, agent_token_for_prompt):
        captured["agent_id"] = agent_id
        captured["token"] = agent_token_for_prompt
        return f"BODY for {agent_id}"

    monkeypatch.setattr(
        agent_tools_module, "generate_system_prompt", _fake_generate
    )
    # log_audit would otherwise touch the DB; the identity contract is
    # what we're pinning here, not the audit write.
    monkeypatch.setattr(agent_tools_module, "log_audit", lambda *a, **k: None)

    principal = make_principal(
        kind="agent_bearer",
        agent_id="worker-7",
        agent_role="worker",
        source_token="bearer-xyz",
    )

    # NO "token" key in arguments — the migration must not need it.
    result = await agent_tools_module.get_system_prompt_tool_impl(
        {}, principal=principal
    )

    text = result[0].text
    assert "worker-7" in text
    assert captured["agent_id"] == "worker-7", (
        "identity must come from principal.agent_id"
    )
    assert captured["token"] == "bearer-xyz", (
        "connection-snippet bearer must come from principal.source_token"
    )
