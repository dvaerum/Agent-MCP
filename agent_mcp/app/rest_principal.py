"""``RestPrincipal`` — the typed identity admitted at the backend REST door.

Finding D (``docs/proposals/security-authz-architecture-hardening.md``,
Phase 5). :func:`agent_mcp.app.deps.require_operator_session` used to
return a bare ``dict[str, Any]`` in one of three undeclared shapes::

    {"kind": "session", "user": ..., "project_role": ..., "sysadmin": ...}
    {"kind": "forwarding", "operator_id": ...}
    {"kind": "operator_bearer", "user": None}

Nothing declared which keys existed on which shape, so every consumer
reached for ``auth.get(...)`` and silently got ``None`` when it guessed
wrong — and the one field that genuinely could not be squeezed into the
"contract-pinned" dict (the forwarding caller's signed role) had to
travel out of band on a module-level ``ContextVar``. This dataclass is
that dict, declared.

Why this is NOT :class:`agent_mcp.core.principal.Principal`
-----------------------------------------------------------
The two types answer different questions and are deliberately kept
distinct (they share a naming convention, not a definition):

* ``RestPrincipal`` is an **admission record**: *which door did this
  caller come through, and what did that door manage to prove about
  them?* Its ``kind`` values (``session`` / ``forwarding`` /
  ``operator_bearer``) are backend-REST doors, not MCP transport modes —
  in particular ``operator_bearer`` has no ``Principal`` analogue that
  means the same thing (the MCP ``agent_bearer`` kind additionally
  distinguishes worker from manager, which this door pre-filters
  upstream in ``deps._is_operator_tier_bearer``; see
  ``core/operator_tier.py`` for why collapsing the two would change who
  is confirmed operator tier).
* ``Principal`` is an **authorization subject**: it carries a resolved
  ``capabilities`` frozenset and is what ``has_capability`` is asked.

There is exactly one conversion between them,
``_dispatch_helpers._build_route_principal``, so "admitted at the REST
door" turns into "authorized to run this tool" in one place rather than
at each of the ~40 REST handlers.

The term to use in prose/comments is **"REST principal"** — the
``CONTEXT.md`` glossary's former "REST auth dict" deviation is closed by
this type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional


#: Which backend-REST door admitted the caller. NOT interchangeable with
#: :data:`agent_mcp.core.principal.PrincipalKind` — see the module
#: docstring and ``CONTEXT.md``'s "PrincipalKind" section.
RestAuthKind = Literal["session", "forwarding", "operator_bearer"]


@dataclass(frozen=True)
class RestPrincipal:
    """Immutable record of who ``require_operator_session`` admitted.

    Attributes:
        kind: The door. ``"session"`` = ``agent_mcp_session`` cookie
            resolving to a live router.db operator session;
            ``"forwarding"`` = a verified, HMAC-signed
            ``X-Agent-MCP-Forwarded-Operator`` header the router
            attaches when proxying a cookie request;
            ``"operator_bearer"`` = an ``Authorization: Bearer`` whose
            ``agents`` row is manager/admin-role and live.
        user: Cookie path only — the router.db ``users`` row behind the
            session. ``None`` on the other two doors (the forwarding
            door sees a signed id, not a row; the bearer door resolves
            an agent, not a user). Kept as the raw row rather than
            projected fields because that is exactly what the previous
            dict carried and what ``caller_identity`` reads.
        operator_id: Forwarding path only — the operator id the router
            signed. Sourced from ``request.state.principal`` (built once
            per request, copy-per-task safe), NEVER from a process-wide
            global: see ``tests/test_sec_r4_operator_identity_race.py``
            for the cross-attribution vulnerability that rule closed.
        project_role: The caller's resolved role in THIS backend's
            project (``"operator"`` / ``"viewer"``), when the door could
            resolve one. ``None`` means "not resolvable here", which
            every consumer must treat as least-privilege, not as
            operator. Always ``None`` on the ``operator_bearer`` door
            (an agent bearer has no project membership row).
        sysadmin: True iff the door resolved a sysadmin identity
            (cookie path via ``group_resolver``, forwarding path via the
            signed principal). Bypasses project-membership checks.
    """

    kind: RestAuthKind
    user: Optional[Mapping[str, Any]] = None
    operator_id: Optional[str] = None
    project_role: Optional[str] = None
    sysadmin: bool = False

    # ── Derived views ────────────────────────────────────────────

    @property
    def username(self) -> Optional[str]:
        """The cookie caller's username, or None on the other doors."""
        user = self.user
        if isinstance(user, Mapping):
            name = user.get("username")
            if name:
                return str(name)
        return None

    def route_role(self) -> Optional[tuple[Optional[str], bool]]:
        """``(project_role, sysadmin)`` to stamp on a dispatched
        ``operator_session`` :class:`~agent_mcp.core.principal.Principal`,
        or ``None`` to keep the historical operator-tier default.

        This is the typed successor to ``deps._forwarding_route_role``,
        the module-level ``ContextVar`` that used to carry these two
        values out of band because "the dispatch helper has no
        Request/auth-dict handle, and the dict's shape is
        contract-pinned elsewhere". Both halves of that excuse are gone:
        the shape is this class, and the handle is ``self``.

        Returning ``None`` for the cookie and bearer doors is
        deliberate, NOT an oversight, and is the behaviour the
        ContextVar had:

        * **Forwarding** — return the REAL signed role. AC-R5-1: a
          forwarding VIEWER must get a viewer-role Principal whose
          capability set the tool's own gate denies, not the full
          operator bundle a hard-coded ``"operator"`` handed them.
        * **Cookie** — ``None``. These paths are genuinely operator-tier
          (``deps._authorize_session_for_project`` 403s a viewer before
          admitting a mutation), but ``project_role`` is legitimately
          ``None`` when the backend cannot reverse-map its own project
          name (ad-hoc / test harness). Threading that ``None`` onto the
          Principal would make ``has_capability`` deny every
          non-``system.*`` cap — a policy change wearing a refactor's
          clothes. The caller keeps its operator-tier default instead.
        * **Operator bearer** — ``None``, same reasoning: the door
          already proved a manager/admin agent row upstream.
        """
        if self.kind == "forwarding":
            return (self.project_role, self.sysadmin)
        return None


__all__ = ["RestAuthKind", "RestPrincipal"]
