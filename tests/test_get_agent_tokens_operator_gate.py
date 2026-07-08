"""Security: ``get_agent_tokens`` must not leak agent bearers to viewers.

FINDING 2 (owner-authorized security review, 2026-07): the
``get_agent_tokens`` MCP tool was gated on the ``agents.view``
capability — which the *viewer* project-role bundle holds
(``core/capabilities.py::PROJECT_ROLE_BUNDLES``) — and
``include_sensitive_data`` DEFAULTED to ``True``. A viewer-tier
operator over the MCP wire could call ``get_agent_tokens {}`` and
receive every agent's plaintext bearer token, then re-authenticate as
those agents to escalate to write.

PR #263 closed the equivalent REST surfaces (``/api/all-data``,
``/api/node-details``, ``/api/tokens``) with
``is_confirmed_operator_tier`` + a masking / allowlist rule in
``app/routers/composition.py`` but missed this MCP tool. This module
pins the MCP tool to the same contract so REST and MCP agree:

  * Gate on an OPERATOR-tier capability (viewers are denied).
  * ``include_sensitive_data`` defaults to ``False``.
  * Plaintext tokens are surfaced ONLY to a confirmed operator-tier
    caller (sysadmin or ``project_role == "operator"``) who explicitly
    opted in — any other caller receives masked tokens regardless of
    the flag, mirroring ``is_confirmed_operator_tier``.
  * The access is audited against the REAL caller, not a hard-coded
    ``"admin"`` actor.
"""

from __future__ import annotations

import json

import pytest

from agent_mcp.core import globals as g
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok, PermissionDenied
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# ── Principal fixtures ───────────────────────────────────────────────


def _operator_principal(user_id: str = "op-user") -> Principal:
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _viewer_principal(user_id: str = "viewer-user") -> Principal:
    """Read-only operator — holds ``agents.view`` via the viewer bundle
    but not the operator-only gate cap."""
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _viewer_with_operator_group_grant(user_id: str = "viewer-grp") -> Principal:
    """A viewer whose group memberships happen to grant the operator-only
    gate cap.

    ``has_capability`` admits the gate cap (the cap is in the set and the
    caller has a project membership), so this principal PASSES the coarse
    gate — but its ``project_role`` is still ``"viewer"``, so it is NOT
    confirmed operator tier and must receive masked tokens. This is the
    defense-in-depth layer that mirrors ``is_confirmed_operator_tier``.
    """
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        capabilities=frozenset({
            "agents.view",
            "tasks.view",
            "memories.view",
            "messages.view",
            "system.view",
            # group-granted operator cap
            "agents.register",
        }),
    )


def _worker_principal(agent_id: str = "wkr") -> Principal:
    return Principal(
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


async def _seed_agent(agent_id: str) -> str:
    """Register an agent and return its plaintext bearer token."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    result = await register_agent_tool_impl(
        {"agent_id": agent_id}, principal=_operator_principal()
    )
    assert isinstance(result, Ok), f"seed register failed: {result!r}"
    token = result.data.get("token")
    assert token, "seed agent must have a token"
    return token


def _result_blob(result: Ok) -> str:
    """Serialise the whole tool result (data + message) so a plaintext
    token leak anywhere in the payload is detectable."""
    return json.dumps(result.data, default=str) + "\n" + (result.message or "")


# ── 1. viewer must never receive plaintext tokens ────────────────────


async def test_viewer_only_caller_never_gets_plaintext_tokens(tmp_path) -> None:
    """A viewer-tier caller (``agents.view`` only) is denied — never
    plaintext. This is the live-reproduced leak."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("victim-a")

        result = await get_agent_tokens_tool_impl(
            {}, principal=_viewer_principal()
        )

    # Denied is the expected outcome (viewer lacks the operator gate cap).
    assert isinstance(result, PermissionDenied), (
        f"viewer must not reach the token payload; got {result!r}"
    )
    # Belt-and-braces: the token must not appear in any denial text.
    assert token not in (result.reason or "")


# ── 2. operator opt-in gets real tokens (no regression) ──────────────


async def test_operator_opt_in_gets_real_tokens(tmp_path) -> None:
    """An operator-tier caller who explicitly opts in still receives the
    plaintext tokens — the legitimate admin path must not regress."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("real-tok")

        result = await get_agent_tokens_tool_impl(
            {"include_sensitive_data": True},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), f"operator must succeed; got {result!r}"
    assert token in _result_blob(result), (
        "confirmed operator opting in must receive the plaintext token"
    )


# ── 3. default masks; non-operator opt-in still masked ───────────────


async def test_operator_default_masks_tokens(tmp_path) -> None:
    """With ``include_sensitive_data`` omitted the default is masked even
    for an operator — the flag now defaults to False."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("masked-default")

        result = await get_agent_tokens_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Ok)
    assert token not in _result_blob(result), (
        "omitting include_sensitive_data must mask the token by default"
    )


async def test_non_operator_tier_opt_in_still_masked(tmp_path) -> None:
    """A caller who passes the coarse gate (group-granted cap) but is not
    confirmed operator tier gets masked tokens even when passing
    ``include_sensitive_data=True`` — mirrors is_confirmed_operator_tier."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("masked-viewer-grp")

        result = await get_agent_tokens_tool_impl(
            {"include_sensitive_data": True},
            principal=_viewer_with_operator_group_grant(),
        )

    assert isinstance(result, Ok), (
        f"group-granted viewer passes the gate; got {result!r}"
    )
    assert token not in _result_blob(result), (
        "non-confirmed-operator-tier caller must be masked despite the flag"
    )


# ── worker rejected (regression guard) ───────────────────────────────


async def test_worker_bearer_rejected(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        result = await get_agent_tokens_tool_impl(
            {}, principal=_worker_principal()
        )

    assert isinstance(result, PermissionDenied)


# ── 4. audit records the real caller, not literal "admin" ────────────


async def test_audit_records_real_caller_not_admin(tmp_path) -> None:
    """The audit entry for the token access must attribute the real
    principal, not a hard-coded ``"admin"`` actor."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        await _seed_agent("audit-src")

        await get_agent_tokens_tool_impl(
            {}, principal=_operator_principal("real-op-42")
        )

        entries = [
            e for e in g.audit_log if e.get("action") == "get_agent_tokens"
        ]

    assert entries, "get_agent_tokens must emit an audit entry"
    assert entries[-1]["agent_id"] == "real-op-42", (
        f"audit must record the real caller; got {entries[-1]['agent_id']!r}"
    )
