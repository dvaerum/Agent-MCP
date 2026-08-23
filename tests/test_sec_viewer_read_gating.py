"""Security: viewer-tier read gating on MCP admin tools.

Owner-authorized defensive review (2026-07-08). Two findings pinned
here:

FINDING 1 [MED] — ``view_status`` and ``view_audit_log`` were gated on
the ``system.view`` capability, which the *viewer* project-role bundle
holds (``core/capabilities.py::PROJECT_ROLE_BUNDLES``). Both tools are
declared ``visibility="operator"`` (hidden from a viewer's
``tools/list``) but that only hides them — a viewer who calls them
directly over the MCP wire still passed the gate and read the FULL
audit log (operator user_ids, every agent action) and every agent's
status + absolute working directory. The gate must be an OPERATOR-tier
capability viewers lack.

FINDING 3 [LOW] — ``get_agent_tokens`` masked bearers for the
non-confirmed-operator path as ``token[:4] + "..." + token[-4:]``,
disclosing 8 characters of a secret bearer. The masked path must
fully redact (``"***"``); the confirmed-operator path still returns
real tokens (the SEC2 contract, unchanged).
"""

from __future__ import annotations

import json

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal fixtures ───────────────────────────────────────────────


def _operator_principal(user_id: str = "op-user") -> Principal:
    return make_principal(
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
    """Read-only operator — holds ``system.view`` via the viewer bundle
    but not the operator-only gate cap."""
    return make_principal(
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


def _sysadmin_principal(user_id: str = "root") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=True,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── FINDING 1: view_status ───────────────────────────────────────────


async def test_view_status_denies_viewer(tmp_path) -> None:
    """A viewer-tier caller (``system.view`` only) must be denied — the
    live-reproduced leak of agent statuses + absolute working dirs.

    R21-F1: the gate is now ``@requires_capability`` (moved off the
    in-body ``_require_capability`` call) — it raises ``AuthRejected``
    instead of returning ``PermissionDenied``. Same denial decision,
    different mechanism.
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        with pytest.raises(AuthRejected):
            await view_status_tool_impl(
                {}, principal=_viewer_principal()
            )


async def test_view_status_allows_operator(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        result = await view_status_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Ok), f"operator must succeed; got {result!r}"


async def test_view_status_allows_sysadmin(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_status_tool_impl

    async with mcp_session(tmp_path):
        result = await view_status_tool_impl(
            {}, principal=_sysadmin_principal()
        )

    assert isinstance(result, Ok), f"sysadmin must succeed; got {result!r}"


# ── FINDING 1: view_audit_log ────────────────────────────────────────


async def test_view_audit_log_denies_viewer(tmp_path) -> None:
    """A viewer-tier caller must be denied — the audit log discloses
    operator user_ids and every agent action.

    R21-F1: raises ``AuthRejected`` now (see test_view_status_denies_viewer).
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools.admin_tools import view_audit_log_tool_impl

    async with mcp_session(tmp_path):
        with pytest.raises(AuthRejected):
            await view_audit_log_tool_impl(
                {}, principal=_viewer_principal()
            )


async def test_view_audit_log_allows_operator(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_audit_log_tool_impl

    async with mcp_session(tmp_path):
        result = await view_audit_log_tool_impl(
            {"limit": 10}, principal=_operator_principal()
        )

    assert isinstance(result, Ok), f"operator must succeed; got {result!r}"


async def test_view_audit_log_allows_sysadmin(tmp_path) -> None:
    from agent_mcp.tools.admin_tools import view_audit_log_tool_impl

    async with mcp_session(tmp_path):
        result = await view_audit_log_tool_impl(
            {"limit": 10}, principal=_sysadmin_principal()
        )

    assert isinstance(result, Ok), f"sysadmin must succeed; got {result!r}"


# ── FINDING 3: masked tokens fully redacted ──────────────────────────


async def _seed_agent(agent_id: str) -> str:
    from agent_mcp.tools.admin_tools import register_agent_tool_impl

    result = await register_agent_tool_impl(
        {"agent_id": agent_id}, principal=_operator_principal()
    )
    assert isinstance(result, Ok), f"seed register failed: {result!r}"
    token = result.data.get("token")
    assert token, "seed agent must have a token"
    return token


async def test_masked_token_fully_redacted_no_prefix_suffix(tmp_path) -> None:
    """The non-confirmed-operator (masked) path must fully redact the
    bearer to ``"***"`` — never disclose the first/last 4 chars."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("mask-target")

        # Operator with default (include_sensitive_data omitted → masked).
        result = await get_agent_tokens_tool_impl(
            {}, principal=_operator_principal()
        )

    assert isinstance(result, Ok), f"operator must succeed; got {result!r}"
    agents = result.data["agents"]
    assert agents, "expected at least one agent row"
    for row in agents:
        masked = row.get("token")
        assert masked == "***", (
            f"masked token must be fully redacted, got {masked!r}"
        )
        # Belt-and-braces: the real bearer must not leak anywhere in the
        # row. Check the FULL token, not a 4-char prefix/suffix fragment —
        # a short fragment can coincidentally collide with digits inside a
        # microsecond timestamp in the serialized row (e.g. suffix "7550"
        # matching "...12.755016"), a false positive that flaked CI. The
        # ``masked == "***"`` assertion above already proves the token
        # field discloses no first/last-N fragment; this guards against
        # the bearer surfacing in any OTHER field.
        assert token not in json.dumps(row), (
            "the real bearer must not leak anywhere in the row"
        )


async def test_confirmed_operator_still_gets_real_tokens(tmp_path) -> None:
    """Regression guard for the SEC2 contract: a confirmed operator who
    opts in still receives the plaintext bearer — the full-mask change
    must not regress the legitimate path."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path):
        token = await _seed_agent("real-target")

        result = await get_agent_tokens_tool_impl(
            {"include_sensitive_data": True},
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok)
    blob = json.dumps(result.data, default=str)
    assert token in blob, "confirmed operator opt-in must see the real token"
