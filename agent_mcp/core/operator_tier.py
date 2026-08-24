"""Single source of truth for "is this caller CONFIRMED operator tier?"

The confirmed-operator-tier predicate answers one security question: may
this caller receive plaintext agent bearer tokens / project secrets, or
must those be masked? It is a defense-in-depth layer BEHIND the coarse
capability gate — a caller who passes the cap gate but whose operator
tier is unverifiable still gets secrets withheld.

Why this module exists
----------------------
The policy was implemented TWICE and the two copies DRIFTED:

  * REST ``app/routers/composition.py::is_confirmed_operator_tier`` keyed
    on the ``require_operator_session`` REST principal's ``kind`` — only a
    per-agent operator-tier bearer (``"operator_bearer"``) was confirmed;
    cookie session / signed forwarding were unverifiable → denied.
  * MCP ``tools/admin_tools.py::_is_confirmed_operator_tier`` keyed on
    ``principal.sysadmin or project_role == "operator"`` — a per-agent
    bearer (``kind == "agent_bearer"``, no ``project_role``) was NOT
    confirmed, while a cookie-session operator WAS.

So the SAME per-agent manager bearer was confirmed on REST yet masked on
MCP — opposite answers on a secret surface. This module is the one
predicate both surfaces call; each adapts its native identity
representation (``RestPrincipal`` / ``Principal``) into these keyword
fields.

The policy, stated once
-----------------------
Confirmed operator tier iff EITHER:

  1. the caller authenticated via a VERIFIABLE per-agent operator-tier
     bearer — REST ``"operator_bearer"`` (``require_operator_session``
     already rejects worker bearers, so it is always manager/admin) or
     MCP ``"agent_bearer"`` with a manager/admin ``agent_role``; OR
  2. the backend can SEE a resolved operator identity — the ``sysadmin``
     flag, or ``project_role == "operator"``.

Cookie-session / signed-forwarding callers are confirmed ONLY through
clause 2, and ONLY when the seam actually supplies the role. Since PR #280
the per-project backend DOES have a router.db role handle: its
``app/deps._authorize_session_for_project`` resolves the cookie caller's
``project_role`` + ``sysadmin`` before admitting, and Wave 12 PR A carries
them on the ``require_operator_session`` REST principal. So the REST
composition seam feeds a real role for the COOKIE (``kind == "session"``)
path — a genuine operator/sysadmin is confirmed and reads their own
project's data, a viewer stays unconfirmed. The signed-FORWARDING path
deliberately passes only ``kind`` here, so a forwarding caller is
conservatively not confirmed at this predicate. Finding D (Phase 5) put
that path's signed role on the REST principal too, but feeding it here
would WIDEN who receives plaintext agent bearers — a policy change, left
as an operator decision; see the scope note on the REST adapter. The MCP ``Principal`` carries a signed role,
so a genuine operator over the wire is confirmed. Any remaining asymmetry
is input availability, not policy drift: one predicate, fed what each seam
can prove.
"""

from __future__ import annotations

from typing import Optional


# Auth-surface discriminators denoting a per-agent operator-tier BEARER:
# REST's ``require_operator_session`` labels it ``"operator_bearer"``; the
# MCP ``Principal`` labels it ``"agent_bearer"``.
_OPERATOR_BEARER_KINDS = frozenset({"operator_bearer", "agent_bearer"})

# Agent roles that count as operator tier for the ``agent_bearer`` path.
# (REST's ``operator_bearer`` is pre-filtered to these upstream; the guard
# only bites the MCP path, where a worker bearer could reach the predicate.)
_OPERATOR_AGENT_ROLES = frozenset({"manager", "admin"})


def is_confirmed_operator_tier(
    *,
    kind: Optional[str],
    sysadmin: bool = False,
    project_role: Optional[str] = None,
    agent_role: Optional[str] = None,
) -> bool:
    """Return True iff this caller is CONFIRMED operator tier.

    See the module docstring for the policy. Callers pass the identity
    fields their seam can prove; absent fields default to the
    least-privilege value (not sysadmin, no role), so a thin seam
    conservatively denies rather than confirming on missing information.
    """
    if kind in _OPERATOR_BEARER_KINDS:
        # REST ``operator_bearer`` is already manager/admin-only and does
        # not carry an ``agent_role``; guard only the MCP ``agent_bearer``
        # path so a worker bearer is not treated as operator tier.
        if kind == "agent_bearer":
            return agent_role in _OPERATOR_AGENT_ROLES
        return True
    if sysadmin:
        return True
    return project_role == "operator"


__all__ = ["is_confirmed_operator_tier"]
