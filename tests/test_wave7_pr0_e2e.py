"""Wave 7 PR 0 + PR 3 — register-only flow (coordinator transition) E2E coverage.

The ``register_agent`` tool mints an agent identity (DB row + bearer
token) and returns a ready-to-paste ``.mcp.json`` snippet that the
operator hands to the user — agent-mcp NEVER starts a claude process
under the coordinator model. PR 3 deleted the legacy ``create_agent``
tool, the runtime tmux helpers, and the dashboard ``CreateAgentModal``
that drove the spawn path. See the Wave 7 section of
``/home/dennis/.claude/plans/prancy-napping-pie.md``.

This file pins, per the plan's "TDD discipline" section:

  1. ``register_agent`` returns ``Ok`` with ``agent_id`` + ``token`` +
     ``mcp_snippet``, and the snippet contains the right URL + bearer.
  2. The minted token authenticates as the new agent end-to-end — a
     ``WorkerSession`` built around the returned token can invoke a
     bearer-gated tool (``view_tasks``) and the call succeeds.
  3. ``terminate_agent`` flips the row to ``terminated`` AND never
     touches a tmux session (PR 3 deleted ``kill_tmux_session``
     entirely along with the ``agent_runtime`` module). The row is
     marked terminated; the token can no longer authenticate.
  4. ``register_agent`` requires an operator-tier principal — calling
     it with an ``agent_bearer`` worker Principal returns
     ``PermissionDenied``.
"""

from __future__ import annotations

import json

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok, PermissionDenied
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _operator_principal(
    project_name: str = "demo-project",
    user_id: str = "test-operator",
) -> Principal:
    """Build an operator-session Principal with a project_name set.

    The register tool's snippet builder reads ``principal.project_name``
    as one of its host-resolution fallbacks; setting it here lets the
    snippet-shape assertions run without the caller also threading the
    project name through ``arguments``.
    """
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=project_name,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _worker_principal(agent_id: str = "wkr") -> Principal:
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="dummy-worker-token",
    )


# ── 1. register-via-dashboard happy path ─────────────────────────────


async def test_register_agent_returns_token_and_snippet(tmp_path) -> None:
    """Operator → register_agent → ``Ok`` carrying agent_id, token,
    and a ``.mcp.json`` snippet whose URL + bearer match the inputs.

    Pins the "operator pastes the snippet into the user's claude" loop
    — the snippet has to be JSON-decodable, name the project in both
    the server key and URL, and embed the minted token in the
    Authorization header.
    """
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {
                "name": "wave7-pr0-happy",
                "role": "worker",
                "host": "https://example.tailnet.ts.net",
            },
            principal=_operator_principal(project_name="demo-project"),
        )

    assert isinstance(result, Ok), f"expected Ok, got {result!r}"
    assert isinstance(result.data, dict)
    assert result.data["agent_id"] == "wave7-pr0-happy"
    assert result.data["agent_role"] == "worker"
    token = result.data["token"]
    assert isinstance(token, str) and token
    snippet_text = result.data["mcp_snippet"]
    assert isinstance(snippet_text, str)

    # The snippet must parse as valid JSON and follow the documented
    # shape: {"mcpServers": {"agent-mcp": {type, url, headers}}}. The
    # server key is the fixed string ``agent-mcp`` (matches the user's
    # .claude.json convention → slash-command prefix ``agent-mcp:``);
    # the project scoping lives in the URL, not the key.
    snippet = json.loads(snippet_text)
    assert "mcpServers" in snippet
    server_key = "agent-mcp"
    assert server_key in snippet["mcpServers"]
    entry = snippet["mcpServers"][server_key]
    assert entry["type"] == "http"
    assert entry["url"] == (
        "https://example.tailnet.ts.net/agent-mcp/mcp/demo-project"
    )
    assert entry["headers"]["Authorization"] == f"Bearer {token}"


async def test_register_agent_falls_back_to_principal_project_name(
    tmp_path,
) -> None:
    """When ``arguments["project_name"]`` is absent the snippet
    builder reads ``principal.project_name`` instead. Mirrors the
    production happy path where the router stamps the project name
    on the Principal at the outermost auth seam."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "wave7-pr0-principal-proj", "host": "https://h.x"},
            principal=_operator_principal(project_name="from-principal"),
        )

    assert isinstance(result, Ok)
    snippet = json.loads(result.data["mcp_snippet"])
    assert "agent-mcp" in snippet["mcpServers"]
    assert (
        snippet["mcpServers"]["agent-mcp"]["url"]
        == "https://h.x/agent-mcp/mcp/from-principal"
    )


# ── 2. register → authenticate-as-agent round-trip ───────────────────


async def test_registered_agent_token_authenticates_as_that_agent(
    tmp_path,
) -> None:
    """End-to-end auth seam: the bearer ``register_agent`` minted can
    be used to call a tool that requires an ``agent_bearer`` Principal
    (``view_tasks``), and the call succeeds.

    Crosses the auth seam end-to-end (per the plan's standing
    constraint: "E2E coverage per PR — end-to-end test that crosses
    the auth seam"). The same wire path real MCP clients take.
    """
    from agent_mcp.tools.admin_tools import register_agent_tool_impl
    from tests.harness import WorkerSession

    async with mcp_session(tmp_path) as admin:
        result = await register_agent_tool_impl(
            {
                "name": "wave7-pr0-roundtrip",
                "role": "worker",
                "host": "https://h.x",
                "project_name": "rt",
            },
            principal=_operator_principal(project_name="rt"),
        )
        assert isinstance(result, Ok)
        agent_id = result.data["agent_id"]
        token = result.data["token"]

        # Build a WorkerSession bound to the freshly-minted token and
        # drive a bearer-gated tool through the registered handlers
        # — same code path real SSE/JSON-RPC clients take.
        wkr = WorkerSession(token=token, agent_id=agent_id, _admin=admin)
        await wkr.assert_tool_succeeds("view_tasks", {})


# ── 3. terminate revokes token + skips tmux ──────────────────────────


async def test_terminate_marks_row_terminated_and_revokes_token(
    tmp_path,
) -> None:
    """``terminate_agent`` flips the row to ``terminated`` AND removes
    the token from the in-memory active_agents cache, so the bearer no
    longer authenticates.

    Plus (Wave 7 coordinator-model invariant): the spawn-era
    ``kill_tmux_session`` helper is GONE from the codebase. PR 0
    dropped the *call*; PR 3 deleted the helper itself along with the
    whole ``agent_runtime`` module. The negative-import assertion
    below pins the architectural contract — agent-mcp must never
    re-grow a tmux-killing surface in ``admin_tools``.
    """
    import importlib

    from agent_mcp.core import globals as g
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    # Negative-import assertion — the spawn-era helper that used to
    # live at ``agent_mcp.runtime.agent_runtime.kill_tmux_session``
    # has no surviving home. ``admin_tools`` doesn't re-export it,
    # and the runtime submodule doesn't exist any more. Loading the
    # module-attr (or the absent submodule) raises.
    admin_tools_mod = importlib.import_module("agent_mcp.tools.admin_tools")
    assert not hasattr(admin_tools_mod, "kill_tmux_session"), (
        "Wave 7 PR 3 deleted kill_tmux_session entirely; "
        "admin_tools should not re-export it."
    )
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_mcp.runtime.agent_runtime")

    async with mcp_session(tmp_path):
        register_result = await register_agent_tool_impl(
            {"name": "wave7-pr0-terminate", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(register_result, Ok)
        token = register_result.data["token"]
        # Sanity: token is in the in-memory cache before terminate.
        assert token in g.active_agents

        term_result = await terminate_agent_tool_impl(
            {"agent_id": "wave7-pr0-terminate"},
            principal=_operator_principal(),
        )

        assert isinstance(term_result, Ok)
        assert term_result.data["agent_id"] == "wave7-pr0-terminate"
        assert term_result.data["status"] == "terminated"

        # Token is evicted from the active-agents cache, so the
        # bearer-injection path used by the harness can't resurrect
        # this agent's identity from it.
        assert token not in g.active_agents

        # Belt-and-braces: the message surfaces the "your local
        # session is still running" guidance the dashboard relays
        # to the operator after a terminate.
        assert (
            term_result.message
            and "local claude session is still running" in term_result.message
        )


# ── 4. operator-only gate ────────────────────────────────────────────


async def test_register_agent_requires_operator_principal(tmp_path) -> None:
    """A worker-tier ``agent_bearer`` principal is rejected with
    :class:`PermissionDenied`. Operator-only enforcement matches every
    other tool in ``admin_tools.py``."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "wave7-pr0-not-allowed"},
            principal=_worker_principal(),
        )

    assert isinstance(result, PermissionDenied)
    assert "operator" in result.reason.lower()


async def test_register_agent_rejects_none_principal(tmp_path) -> None:
    """Unauthenticated call (no Principal threaded through) collapses
    to :class:`PermissionDenied`. Matches the rest of admin_tools."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "wave7-pr0-no-principal"},
            principal=None,
        )

    assert isinstance(result, PermissionDenied)


# ── 5. snippet shape extras (async to share the module pytestmark) ──


async def test_register_agent_snippet_handles_missing_project(tmp_path) -> None:
    """When the principal has no project_name AND the caller didn't
    pass one explicitly, the snippet falls back to the single-server-
    key shape with the ``/mcp`` URL — matches the pre-router single-
    tenant client config."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {"name": "wave7-pr0-no-project", "host": "https://h"},
            principal=_operator_principal(project_name=None),
        )

    assert isinstance(result, Ok)
    snippet = json.loads(result.data["mcp_snippet"])
    assert "agent-mcp" in snippet["mcpServers"]
    entry = snippet["mcpServers"]["agent-mcp"]
    assert entry["url"] == "https://h/mcp"
    token = result.data["token"]
    assert entry["headers"]["Authorization"] == f"Bearer {token}"


async def test_register_agent_strips_trailing_slash_from_host(tmp_path) -> None:
    """``_resolve_snippet_host`` strips a trailing ``/`` so the URL
    concatenation in the snippet doesn't produce a double-slash. The
    dashboard's ``window.location.origin`` never has a trailing
    slash, but operator-supplied values from the CLI / future surfaces
    might — this is cheap insurance."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    async with mcp_session(tmp_path):
        result = await register_agent_tool_impl(
            {
                "name": "wave7-pr0-trailing-slash",
                "host": "https://h.x/",
                "project_name": "p",
            },
            principal=_operator_principal(project_name="p"),
        )

    assert isinstance(result, Ok)
    snippet = json.loads(result.data["mcp_snippet"])
    assert (
        snippet["mcpServers"]["agent-mcp"]["url"]
        == "https://h.x/agent-mcp/mcp/p"
    )
