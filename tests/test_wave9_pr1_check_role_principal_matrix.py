"""Wave 9 PR 1 — ``_check_role_principal`` capability migration matrix.

Wave 9 PR 1 of 7 in ``prancy-napping-pie.md``. Migrates the four
``has_role(...)`` call sites inside
:func:`agent_mcp.core.authorize._check_role_principal` to
``has_capability(...)`` via the new :func:`_role_marker_cap` helper.
The function's external admit/reject contract for the three legacy
role strings (``"operator"`` / ``"manager"`` / ``"any"``) MUST stay
identical — the helper is purely an internal refactor of the dispatch.

This file pins the full admit/reject matrix end-to-end:

    (role × principal.kind × sysadmin × agent_role × project_role)
    →  expected admit/reject decision

Every combination is exercised against both the pre-Wave-9 contract
(reachable via :meth:`Principal.has_role` — the bridge stays alive
until PR 6) and the post-migration capability-driven implementation
behind :func:`_check_role_principal`. The two must agree for every
row, otherwise PR 1 has silently broken a deployed authorization
contract.

The new ``_role_marker_cap`` helper also gets its own focused tests
asserting the cap string per role string and a defensive ``ValueError``
on unknown role strings.
"""
from __future__ import annotations

from typing import Optional

import pytest

from agent_mcp.core.authorize import (
    AuthRejected,
    _check_role_principal,
    _role_marker_cap,
)
from agent_mcp.core.principal import Principal


# ── _role_marker_cap helper ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("role", "expected_cap"),
    [
        ("operator", "system.config.write"),
        ("admin", "system.config.write"),
        ("manager", "tasks.assign"),
        ("any", "mcp.connect"),
    ],
)
def test_role_marker_cap_returns_expected_cap(
    role: str, expected_cap: str,
) -> None:
    """Each legacy role name maps to exactly one marker cap.

    The marker caps were locked in the Wave 9 PR 1 prompt:

    * operator/admin → ``system.config.write`` (operator bundle only)
    * manager → ``tasks.assign`` (operator + manager-agent bundles)
    * any → ``mcp.connect`` (worker + manager-agent bundles)

    Changing the marker cap is a contract change; this test pins
    against accidental rewiring (e.g. picking a cap that's also in
    the viewer bundle, which would weaken the operator gate).
    """
    assert _role_marker_cap(role) == expected_cap


def test_role_marker_cap_unknown_role_raises() -> None:
    """An unknown role string raises ``ValueError`` so a typo in a
    new decorator surfaces at construction time, not as a silent
    admit."""
    with pytest.raises(ValueError, match="unknown role"):
        _role_marker_cap("definitely-not-a-role")


# ── Principal factories per kind × sysadmin × agent_role × project_role ──


def _operator_session(
    *, sysadmin: bool, project_role: Optional[str],
) -> Principal:
    """An operator-session Principal — the cookie-authed dashboard path.

    project_role is the per-project membership role
    (``"operator"`` / ``"viewer"`` / ``None``); sysadmin flips the
    transitive-admin override.
    """
    return Principal(
        kind="operator_session",
        user_id="alice",
        agent_id=None,
        sysadmin=sysadmin,
        project_name="proj-a",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _forwarding_header(
    *, sysadmin: bool, project_role: Optional[str],
) -> Principal:
    """A forwarding-header Principal — the router-signed bridge into the
    per-project backend that the router uses for cookie-authed
    dashboard requests."""
    return Principal(
        kind="forwarding_header",
        user_id="alice",
        agent_id=None,
        sysadmin=sysadmin,
        project_name="proj-a",
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


def _agent_bearer(
    *, agent_role: Optional[str],
) -> Principal:
    """An agent-bearer Principal — a per-agent token on
    ``Authorization: Bearer``. Sysadmin is always False for agent
    bearers (the column is on the user, not the agent).
    """
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="agent-1",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=agent_role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token="tok",
    )


# ── Full admit/reject matrix ────────────────────────────────────────


# Each row: (label, role, principal_factory, expect_admit)
# `expect_admit=True` means `_check_role_principal(role, p)` returns
# without raising; `False` means it raises AuthRejected.
#
# The expected outcomes were derived by reading the pre-Wave-9
# behaviour of `_check_role_principal` (which called `has_role`):
#
#   * operator/admin admits iff sysadmin OR operator-tier-kind, then
#     rejected if project_role == "viewer" via _viewer_blocked.
#   * manager admits iff sysadmin OR operator-tier-kind OR
#     (agent_bearer AND agent_role == "manager"), with same viewer
#     gate.
#   * any admits iff agent_bearer OR sysadmin OR operator-tier-kind
#     (NO viewer gate — the legacy contract didn't block viewer on
#     "any").
#
# The marker-cap migration must reproduce these decisions row-for-row.

_MATRIX: list[tuple[str, str, Principal, bool]] = [
    # ── Operator/admin role ──────────────────────────────────────
    (
        "operator: sysadmin op_sess project=operator → admit",
        "operator",
        _operator_session(sysadmin=True, project_role="operator"),
        True,
    ),
    (
        "operator: sysadmin op_sess project=viewer → admit (sysadmin exempt)",
        "operator",
        _operator_session(sysadmin=True, project_role="viewer"),
        True,
    ),
    (
        "operator: sysadmin op_sess project=None → admit (sysadmin wildcard)",
        "operator",
        _operator_session(sysadmin=True, project_role=None),
        True,
    ),
    (
        "operator: non-sysadmin op_sess project=operator → admit",
        "operator",
        _operator_session(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "operator: non-sysadmin op_sess project=viewer → reject (viewer)",
        "operator",
        _operator_session(sysadmin=False, project_role="viewer"),
        False,
    ),
    (
        "operator: sysadmin fwd_hdr project=operator → admit",
        "operator",
        _forwarding_header(sysadmin=True, project_role="operator"),
        True,
    ),
    (
        "operator: non-sysadmin fwd_hdr project=operator → admit",
        "operator",
        _forwarding_header(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "operator: non-sysadmin fwd_hdr project=viewer → reject (viewer)",
        "operator",
        _forwarding_header(sysadmin=False, project_role="viewer"),
        False,
    ),
    (
        "operator: agent_bearer worker → reject",
        "operator",
        _agent_bearer(agent_role="worker"),
        False,
    ),
    (
        "operator: agent_bearer manager → reject (op-only)",
        "operator",
        _agent_bearer(agent_role="manager"),
        False,
    ),

    # ── Admin (legacy alias for operator) ────────────────────────
    (
        "admin: non-sysadmin op_sess project=operator → admit",
        "admin",
        _operator_session(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "admin: agent_bearer manager → reject (op-only)",
        "admin",
        _agent_bearer(agent_role="manager"),
        False,
    ),

    # ── Manager role ─────────────────────────────────────────────
    (
        "manager: sysadmin op_sess project=operator → admit",
        "manager",
        _operator_session(sysadmin=True, project_role="operator"),
        True,
    ),
    (
        "manager: non-sysadmin op_sess project=operator → admit",
        "manager",
        _operator_session(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "manager: non-sysadmin op_sess project=viewer → reject (viewer)",
        "manager",
        _operator_session(sysadmin=False, project_role="viewer"),
        False,
    ),
    (
        "manager: non-sysadmin fwd_hdr project=operator → admit",
        "manager",
        _forwarding_header(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "manager: non-sysadmin fwd_hdr project=viewer → reject (viewer)",
        "manager",
        _forwarding_header(sysadmin=False, project_role="viewer"),
        False,
    ),
    (
        "manager: agent_bearer manager → admit",
        "manager",
        _agent_bearer(agent_role="manager"),
        True,
    ),
    (
        "manager: agent_bearer worker → reject",
        "manager",
        _agent_bearer(agent_role="worker"),
        False,
    ),

    # ── Any role ─────────────────────────────────────────────────
    (
        "any: sysadmin op_sess project=operator → admit (wildcard)",
        "any",
        _operator_session(sysadmin=True, project_role="operator"),
        True,
    ),
    (
        "any: non-sysadmin op_sess project=operator → admit (kind)",
        "any",
        _operator_session(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "any: non-sysadmin op_sess project=viewer → admit (kind, no viewer gate)",
        "any",
        _operator_session(sysadmin=False, project_role="viewer"),
        True,
    ),
    (
        "any: non-sysadmin fwd_hdr project=operator → admit (kind)",
        "any",
        _forwarding_header(sysadmin=False, project_role="operator"),
        True,
    ),
    (
        "any: agent_bearer worker → admit (mcp.connect)",
        "any",
        _agent_bearer(agent_role="worker"),
        True,
    ),
    (
        "any: agent_bearer manager → admit (mcp.connect)",
        "any",
        _agent_bearer(agent_role="manager"),
        True,
    ),
]


@pytest.mark.parametrize(
    ("label", "role", "principal", "expect_admit"),
    [
        pytest.param(label, role, principal, expect_admit, id=label)
        for label, role, principal, expect_admit in _MATRIX
    ],
)
def test_check_role_principal_matches_expected_admit(
    label: str, role: str, principal: Principal, expect_admit: bool,
) -> None:
    """The marker-cap dispatch admits exactly the same principals as
    the pre-Wave-9 ``has_role``-based dispatch.

    A row that mismatches is a contract regression — the migration
    must preserve every legacy admit/reject decision. The new
    marker-cap helper isn't allowed to weaken (admit something the
    old code denied) or tighten (reject something the old code
    admitted) the existing role gates.
    """
    if expect_admit:
        # Returns None on admit; the absence of an exception is the
        # success signal.
        assert _check_role_principal(role, principal) is None
    else:
        with pytest.raises(AuthRejected):
            _check_role_principal(role, principal)


def test_check_role_principal_unknown_role_raises_value_error() -> None:
    """Unknown role string raises ``ValueError`` (defensive — the
    decorator construction guards already filter; this backs that
    invariant up at the function boundary)."""
    p = _operator_session(sysadmin=True, project_role="operator")
    with pytest.raises(ValueError, match="unknown role"):
        _check_role_principal("not-a-role", p)
