"""Pentest R8-F1 — ``config_aoe_*`` writes are sysadmin-only.

The AoE (Agents-of-Empires) notify feature
(``agent_mcp/features/aoe_notify.py``) reads a handful of
``config_aoe_*`` project-context keys to build a server-side OUTBOUND
httpx client: ``config_aoe_base_url`` becomes the request ``base_url``
and ``config_aoe_bearer_token`` becomes its ``Authorization`` header.

Before this fix ANY ``config_*`` key was writable at the per-project
OPERATOR tier (``PROJECT_ROLE_BUNDLES["operator"]`` carries
``system.config.write``; the ownership matrix short-circuits admins
past the ``config_*`` worker-block). That let a per-project operator
point the shared host's outbound request at an operator-chosen address
(internal / link-local / metadata → SSRF) and exfiltrate the configured
bearer to it.

AoE is a MACHINE-level integration (default ``http://127.0.0.1:8181``);
deciding WHERE the host points it is a host-owner (sysadmin) decision,
not a per-project operator's. So ``config_aoe_*`` create / update /
delete now requires sysadmin. Other ``config_*`` keys (worker-policy
toggles, event-loop policy) stay operator-writable — this change is
scoped to the ``config_aoe_`` prefix only.

RED (on origin/main): the operator-denied tests below SUCCEED (the
operator write lands as ``Ok``). After the fix they return
``PermissionDenied``. The sysadmin-allowed + non-AoE-operator-allowed
tests are the GREEN guardrails that keep the change tightly scoped.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok, PermissionDenied
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal builders ───────────────────────────────────────────


def _operator_principal() -> Principal:
    """Per-project operator (cookie/session), NOT sysadmin.

    Carries ``system.config.write`` via the operator bundle, so it
    passes the ownership-matrix ``config_*`` gate — this is exactly the
    tier that must NO LONGER be able to write ``config_aoe_*``.
    """
    return make_principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=False,
        project_name="demo",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _sysadmin_principal() -> Principal:
    """Top-level sysadmin (host owner) — carries the capability wildcard."""
    return make_principal(
        kind="operator_session",
        user_id="root",
        agent_id=None,
        sysadmin=True,
        project_name=None,
        project_role=None,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── operator DENIED on config_aoe_* (RED before the fix) ─────────


async def test_operator_denied_creating_config_aoe_base_url(tmp_path) -> None:
    """A per-project operator setting ``config_aoe_base_url`` (the
    outbound request target) is DENIED. On origin/main this SUCCEEDS —
    that is the RED this pentest fix flips."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {
                "context_key": "config_aoe_base_url",
                "context_value": "http://169.254.169.254/latest/meta-data/",
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied), (
        f"operator must not set config_aoe_base_url, got {result!r}"
    )
    assert "config_aoe" in result.reason or "sysadmin" in result.reason.lower()


async def test_operator_denied_creating_config_aoe_notify_enabled(
    tmp_path,
) -> None:
    """A second ``config_aoe_*`` key (``config_aoe_notify_enabled``) is
    likewise operator-denied — proves the gate is the whole prefix, not
    just the base_url key."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {"context_key": "config_aoe_notify_enabled", "context_value": True},
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied), (
        f"operator must not set config_aoe_notify_enabled, got {result!r}"
    )


async def test_operator_denied_creating_config_aoe_uppercase(tmp_path) -> None:
    """The gate is case-insensitive (``CONFIG_AOE_BASE_URL``)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {"context_key": "CONFIG_AOE_BASE_URL", "context_value": "http://x"},
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied)


async def test_operator_denied_bulk_update_with_config_aoe(tmp_path) -> None:
    """A bulk update whose batch contains a ``config_aoe_*`` key is
    rejected wholesale (bulk write path)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "config_aoe_timeout_ms", "context_value": 5000},
                ]
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied)


async def test_operator_denied_create_project_context_config_aoe(
    tmp_path,
) -> None:
    """The INSERT-only ``create_project_context`` path is gated too."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "create_project_context",
            {"context_key": "config_aoe_bearer_token", "context_value": "s3cr3t"},
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied)


async def test_operator_denied_deleting_config_aoe(tmp_path) -> None:
    """An operator cannot DELETE a ``config_aoe_*`` key either — a
    sysadmin seeds it first, then the operator's delete is denied."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        seeded = await dispatch_tool_call(
            "update_project_context",
            {
                "context_key": "config_aoe_base_url",
                "context_value": "http://127.0.0.1:8181",
            },
            principal=_sysadmin_principal(),
        )
        assert isinstance(seeded, Ok)

        result = await dispatch_tool_call(
            "delete_project_context",
            {"context_key": "config_aoe_base_url"},
            principal=_operator_principal(),
        )

    assert isinstance(result, PermissionDenied), (
        f"operator must not delete config_aoe_base_url, got {result!r}"
    )


# ── sysadmin STILL allowed on config_aoe_* (GREEN guardrail) ─────


async def test_sysadmin_allowed_creating_config_aoe_base_url(tmp_path) -> None:
    """A sysadmin configures the machine-level integration normally."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {
                "context_key": "config_aoe_base_url",
                "context_value": "http://127.0.0.1:8181",
            },
            principal=_sysadmin_principal(),
        )

    assert isinstance(result, Ok), (
        f"sysadmin must be able to set config_aoe_base_url, got {result!r}"
    )


# ── scope guardrail — non-AoE config_* stays operator-writable ───


async def test_operator_still_writes_non_aoe_config_key(tmp_path) -> None:
    """The change is scoped to ``config_aoe_*`` ONLY: a per-project
    operator can still write other ``config_*`` policy keys (here a
    worker-policy toggle)."""
    from agent_mcp.tools.registry import dispatch_tool_call

    async with mcp_session(tmp_path):
        result = await dispatch_tool_call(
            "update_project_context",
            {
                "context_key": "config_allow_worker_r8_probe",
                "context_value": True,
            },
            principal=_operator_principal(),
        )

    assert isinstance(result, Ok), (
        f"operator must still write non-AoE config_* keys, got {result!r}"
    )
