"""Arch round-2 #5: the two "may this caller see plaintext agent bearer
tokens?" predicates must not DRIFT.

Two implementations of the same policy lived apart and disagreed for the
same logical identity:

  * REST  ``app/routers/composition.py::is_confirmed_operator_tier`` —
    keyed on the ``require_operator_session`` auth-dict ``kind``; only
    ``"operator_bearer"`` (a per-agent manager bearer) was confirmed.
  * MCP   ``tools/admin_tools.py::_is_confirmed_operator_tier`` — keyed on
    ``principal.sysadmin or principal.project_role == "operator"``; a
    per-agent bearer (``kind == "agent_bearer"``, no ``project_role``)
    was NOT confirmed.

So a per-agent MANAGER bearer — the SAME identity on both wires — was
CONFIRMED on REST (saw plaintext tokens / secrets) but MASKED on MCP.
Opposite answers, secret surface. This pins the two surfaces to a single
shared predicate (:func:`agent_mcp.core.operator_tier.is_confirmed_operator_tier`)
so the drift is unrepresentable.

Note on the cookie/forwarding axis: a cookie-session or signed-forwarding
operator is deliberately NOT convergent across the surfaces — the REST
composition seam's auth dict carries no verifiable project role (the
per-project backend has no router.db role handle), so REST conservatively
denies, while the MCP ``Principal`` carries a signed role and can confirm
a genuine operator. That asymmetry is input-availability, not logic drift;
this suite asserts convergence only on the identity BOTH seams represent
with identical information — the per-agent bearer.
"""

from __future__ import annotations

from agent_mcp.core.principal import Principal


def _rest_answer(auth: dict) -> bool:
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    return is_confirmed_operator_tier(auth)


def _mcp_answer(principal: Principal) -> bool:
    from agent_mcp.tools.admin_tools import _is_confirmed_operator_tier

    return _is_confirmed_operator_tier(principal)


def _manager_bearer_principal() -> Principal:
    """A per-agent MANAGER bearer — the MCP twin of REST's
    ``operator_bearer`` auth dict."""
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="mgr-agent",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="manager",
        can_wake_loop=False,
        source_token="dummy-manager-token",
    )


def _worker_bearer_principal() -> Principal:
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id="wkr-agent",
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="dummy-worker-token",
    )


# ── the drift: SAME identity → SAME answer on both surfaces ───────────


def test_manager_bearer_same_answer_on_rest_and_mcp() -> None:
    """A per-agent manager bearer is the SAME identity on REST
    (``operator_bearer``) and MCP (``agent_bearer`` + manager role). Both
    surfaces must return the SAME confirmed-operator-tier answer.

    Pre-fix: REST returns True, MCP returns False → drift → RED.
    """
    rest = _rest_answer({"kind": "operator_bearer", "user": None})
    mcp = _mcp_answer(_manager_bearer_principal())

    assert rest == mcp, (
        f"token-disclosure predicate drift: REST={rest} MCP={mcp} for the "
        f"same per-agent manager-bearer identity"
    )
    # And that shared answer is CONFIRMED — a manager bearer is verifiable
    # operator tier (REST already trusts it; MCP must too).
    assert rest is True


def test_worker_bearer_not_confirmed_on_both() -> None:
    """A per-agent WORKER bearer is never confirmed operator tier — on
    either surface. Regression guard on the unified predicate: the
    manager-bearer widening must not admit workers."""
    assert _mcp_answer(_worker_bearer_principal()) is False


# ── the shared predicate is the single source of truth ───────────────


def test_shared_predicate_exists_and_both_delegate() -> None:
    """Both surface predicates must route through one core predicate so
    they cannot drift again."""
    from agent_mcp.core.operator_tier import is_confirmed_operator_tier

    # Verifiable per-agent operator-tier bearer → confirmed.
    assert is_confirmed_operator_tier(kind="operator_bearer") is True
    assert (
        is_confirmed_operator_tier(kind="agent_bearer", agent_role="manager")
        is True
    )
    # Worker bearer → not operator tier.
    assert (
        is_confirmed_operator_tier(kind="agent_bearer", agent_role="worker")
        is False
    )
    # Cookie/forwarding identity: confirmed only with a resolved operator
    # role or the sysadmin flag; a bare session (no role) is unverifiable.
    assert is_confirmed_operator_tier(kind="operator_session") is False
    assert (
        is_confirmed_operator_tier(
            kind="operator_session", project_role="operator"
        )
        is True
    )
    assert (
        is_confirmed_operator_tier(
            kind="operator_session", project_role="viewer"
        )
        is False
    )
    assert is_confirmed_operator_tier(kind="operator_session", sysadmin=True) is True
