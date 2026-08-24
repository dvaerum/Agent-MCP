"""ADR-0025 invariant: the forwarding door is NEVER confirmed operator tier.

``composition.is_confirmed_operator_tier`` deliberately feeds ONLY
``kind`` to the shared predicate for ``kind == "forwarding"`` — never the
signed ``project_role`` / ``sysadmin`` the Phase 5 ``RestPrincipal``
carries. A forwarding caller therefore never receives plaintext agent
bearer tokens from ``GET /api/tokens`` or ``GET /api/all-data``,
regardless of the (unforgeable, HMAC-covered) role it presents.

This is NOT a bug repro — the code already does the right thing. It is
the permanent regression pin for the near-miss Phase 5 caught once: the
one-line "fix" of threading ``project_role``/``sysadmin`` through the
forwarding branch looks like completing a refactor and is actually a
policy widening. See:

  * ``docs/adr/0025-forwarding-tier-excluded-from-confirmed-operator-tier.md``
    — the decision, and the sanctioned escape hatch if it is ever
    revisited (an explicit off-by-default config flag, NOT an edit to
    the predicate);
  * ``agent_mcp/app/routers/composition.py::is_confirmed_operator_tier``
    — the "DELIBERATE SCOPE (Finding D, Phase 5)" note;
  * ``agent_mcp/app/forwarding_header.py`` — the module docstring whose
    "Why not just include a nonce" reasoning bounds REPLAY risk only,
    and so does not license disclosure trust.

Strictly broader than ``test_wave12_pra_operator_tier.py``'s single
all-defaults assertion, which that file keeps: this crosses every
``project_role`` with both ``sysadmin`` values, including the
``("operator", True)`` combination the naive edit would flip to True.
"""

from __future__ import annotations

import itertools

import pytest

from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.app.routers.composition import is_confirmed_operator_tier

# Every role the forwarding header can legitimately sign, plus the
# "seam could not resolve one" case. ``forwarding_header``'s known
# project-role set is {"operator", "viewer"}; ``None`` is the
# unresolvable/least-privilege value ``RestPrincipal`` defaults to.
_PROJECT_ROLES = ("operator", "viewer", None)
_SYSADMIN_FLAGS = (True, False)


@pytest.mark.parametrize(
    "project_role,sysadmin",
    list(itertools.product(_PROJECT_ROLES, _SYSADMIN_FLAGS)),
)
def test_forwarding_never_confirmed_operator_tier(project_role, sysadmin):
    """No (project_role, sysadmin) combination confirms a forwarding caller."""
    auth = RestPrincipal(
        kind="forwarding",
        operator_id="op-1",
        project_role=project_role,
        sysadmin=sysadmin,
    )

    assert is_confirmed_operator_tier(auth) is False


def test_forwarding_operator_sysadmin_is_the_regression_case():
    """The exact combination a one-line 'finish the refactor' would flip.

    Called out on its own so a future reader sees the case by name and
    not only as one parametrised id: a forwarding caller carrying BOTH a
    signed ``project_role == "operator"`` AND ``sysadmin=True`` — i.e.
    the most privileged identity the door can assert — is still not
    confirmed operator tier for secrets disclosure.
    """
    auth = RestPrincipal(
        kind="forwarding",
        operator_id="op-1",
        project_role="operator",
        sysadmin=True,
    )

    assert is_confirmed_operator_tier(auth) is False


def test_forwarding_role_is_still_carried_on_the_principal():
    """The exclusion is at the PREDICATE, not by dropping the role.

    ``RestPrincipal.route_role()`` must keep returning the real signed
    role for the forwarding door (AC-R5-1: a forwarding VIEWER gets a
    viewer-capability Principal). If a future edit "fixed" this test by
    zeroing the fields instead of by leaving the predicate alone, that
    would silently re-open the viewer→operator escalation the signed
    role closed — so pin both halves together.
    """
    auth = RestPrincipal(
        kind="forwarding",
        operator_id="op-1",
        project_role="viewer",
        sysadmin=False,
    )

    assert auth.route_role() == ("viewer", False)
    assert is_confirmed_operator_tier(auth) is False


@pytest.mark.parametrize("project_role", _PROJECT_ROLES)
@pytest.mark.parametrize("sysadmin", _SYSADMIN_FLAGS)
def test_non_forwarding_doors_are_unaffected(project_role, sysadmin):
    """The exclusion is scoped to the forwarding door only.

    Guards against an over-broad "fix" that makes the predicate ignore
    ``project_role``/``sysadmin`` everywhere: the cookie door must still
    confirm a genuine operator or sysadmin.
    """
    session = RestPrincipal(
        kind="session",
        project_role=project_role,
        sysadmin=sysadmin,
    )

    expected = sysadmin or project_role == "operator"
    assert is_confirmed_operator_tier(session) is expected
