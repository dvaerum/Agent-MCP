"""Principal — the typed identity of "who is making this call".

Wave 6 PR 0 of 7 (retire-system-token follow-up). See the Wave 6
and Wave 9 sections of
``/home/dennis/.claude/plans/prancy-napping-pie.md`` for the full
design context.

Why this exists
---------------
Today, "who is the caller?" is reconstructed in five different
places (``router/auth_middleware.py``, ``app/main_app.py``,
``app/deps.py``, ``app/routes.py``, ``core/authorize.py``) by
walking ContextVars and re-running ``verify_token`` / ``get_agent_id``
chains. Each surface invents the same answer slightly differently;
the policy decisions ("is this caller allowed to assign tasks?")
read from the shape of who-is-calling rather than asking it
directly.

``Principal`` collapses those five derivations into one: every
middleware that has enough information to identify the caller
builds a ``Principal`` once, stashes it on the request, and the
tool dispatcher threads it through to every tool implementation.
Per-tool authorization becomes a method call on the Principal
(``principal.has_capability("tasks.assign")``).

Wave 9 PR 6 deleted the legacy ``has_role()`` bridge and the
deprecated ``@requires`` / ``@requires_role`` decorators; the
capability vocabulary (:meth:`Principal.has_capability` consulting
:data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`) is now the
single authorization surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


PrincipalKind = Literal["operator_session", "agent_bearer", "forwarding_header"]
AgentRole = Literal["worker", "manager"]


# Sentinel used to distinguish "caller did not pass capabilities="
# from "caller passed an explicitly empty set". Module-private; never
# stored on a Principal. Wave 9 PR 0 — when the migration window
# closes in PR 6 the dataclass switches to a required field and the
# sentinel disappears.
_CAPS_UNSET: frozenset[str] = frozenset({"__capabilities_unset_sentinel__"})


@dataclass(frozen=True)
class Principal:
    """Immutable snapshot of the caller's authenticated identity.

    Built once at the outermost seam (the auth middleware that
    admitted the request) and threaded through every downstream
    decision point. Read-only by design — a per-tool authorization
    rule operates on the Principal it was handed; it never mutates
    or re-derives a different identity halfway through a request.

    Attributes:
        kind: Which authentication surface admitted the caller.
            ``"operator_session"`` is the dashboard cookie path,
            ``"agent_bearer"`` is a per-agent token on
            ``Authorization: Bearer``, ``"forwarding_header"`` is
            the signed ``X-Agent-MCP-Forwarded-Operator`` header
            the router attaches when proxying a cookie-authenticated
            dashboard request to the per-project backend.
        user_id: Operator id (``users.user_id`` for cookie path,
            the signed operator id for forwarding-header path).
            None for ``"agent_bearer"``.
        agent_id: Agent id (``agents.agent_id``). None for both
            operator paths.
        sysadmin: True iff this user is a sysadmin (cookie /
            forwarding-header path), allowing project-membership
            bypass. Always False for ``"agent_bearer"``.
        project_name: The project this request targets, derived
            from the URL path. None for router-admin endpoints
            (``/api/router/...``).
        project_role: For ``"operator_session"`` /
            ``"forwarding_header"``, the operator's role inside
            ``project_name`` (``"operator"`` / ``"viewer"`` / None
            when no membership row exists and the caller isn't a
            sysadmin). None for ``"agent_bearer"``.
        agent_role: For ``"agent_bearer"``, the row's
            ``agents.agent_role`` (``"worker"`` or ``"manager"``).
            None for the operator paths.
        can_wake_loop: True iff the wake-loop bootstrap instruction
            should be appended to this caller's ``initialize``
            response. Tracks the same logic
            ``_bearer_has_wake_loop_enabled`` (``app/main_app.py``)
            inlines today; resolved once at middleware time so
            downstream consumers don't re-query.
        source_token: For bearer-authed callers, the raw bearer
            value (kept around so in-process callbacks like
            ``send_agent_message`` can re-attach the same bearer
            on synthesized sub-calls without reaching back through
            ContextVars). For SSO callers, the provider name
            (audit-log attribution). None when neither applies.
    """

    kind: PrincipalKind
    user_id: Optional[str]
    agent_id: Optional[str]
    sysadmin: bool
    project_name: Optional[str]
    project_role: Optional[str]
    agent_role: Optional[AgentRole]
    can_wake_loop: bool
    source_token: Optional[str]
    # Wave 9 PR 0: capability set resolved at middleware time. Default
    # is the ``_CAPS_UNSET`` sentinel so ``__post_init__`` can detect
    # "caller did not supply" and back-fill via
    # ``resolve_capabilities`` from the identity fields. Pre-Wave-9
    # construction sites (~30 places across tests, app routers, and
    # in-process synthesis) keep working unchanged — they get the
    # capabilities their identity shape implies. Explicit
    # ``capabilities=frozenset(...)`` overrides the back-fill so the
    # auth middleware can pass the result of ``resolve_capabilities``
    # (which also consults group caps from router.db) verbatim.
    capabilities: frozenset[str] = field(default=_CAPS_UNSET)

    def __post_init__(self) -> None:
        """Back-fill ``capabilities`` from identity fields when unset.

        Wave 9 PR 0 — bridge: pre-Wave-9 Principal construction sites
        don't yet pass ``capabilities=``. Rather than touch every one
        of them in PR 0 (the scope is foundation + bridge, not call-
        site migration), we resolve from the identity shape here. The
        production middleware path already calls
        :func:`resolve_capabilities` with the full identity + group
        context and passes the result explicitly, so this back-fill
        only fires for in-process synthesis (tests, dispatcher
        fallbacks).
        """
        if self.capabilities is _CAPS_UNSET:
            from .capabilities import resolve_capabilities
            object.__setattr__(
                self,
                "capabilities",
                resolve_capabilities(
                    user_id=self.user_id,
                    agent_id=self.agent_id,
                    sysadmin=self.sysadmin,
                    agent_role=self.agent_role,
                    project_role=self.project_role,
                    kind=self.kind,
                ),
            )

    # ── Authorization helpers ────────────────────────────────────

    def has_capability(self, cap: str) -> bool:
        """Return True iff this principal carries ``cap``.

        Wave 9 PR 0 — the new capability gate. Per the plan's
        pseudocode:

        * Sysadmin short-circuit: a ``SYSADMIN_WILDCARD`` in
          ``self.capabilities`` admits ANY cap unconditionally. The
          wildcard is how :func:`resolve_capabilities` encodes
          sysadmin — one sentinel instead of materialising all 27 caps
          onto every sysadmin Principal.
        * Otherwise the cap must be in ``self.capabilities``.
        * For non-``system.*`` caps, additionally require the caller
          to have a project membership (``project_role is not None``)
          OR be an ``agent_bearer``. ``system.*`` caps are
          router-admin verbs and don't belong to any one project —
          they admit on cap-set membership alone.

        Returns False for any cap not in the in-memory set, including
        unknown / typo'd cap strings — default-deny matches the
        plan's design (typos surface at code-review via the
        :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`
        smoke test, not at runtime).
        """
        from .capabilities import SYSADMIN_WILDCARD

        if SYSADMIN_WILDCARD in self.capabilities:
            return True
        if cap not in self.capabilities:
            return False
        if cap.startswith("system."):
            return True
        return self.project_role is not None or self.kind == "agent_bearer"

    # ── Audit-log attribution helper ─────────────────────────────

    def actor_label(self) -> str:
        """Return a short string suitable for an audit-log ``agent_id`` column.

        Picks the most specific identifier available: ``agent_id``
        for agent bearers, ``user_id`` for operator paths. Falls
        back to the kind label if neither is set (shouldn't happen
        in practice — defensive).
        """
        if self.agent_id:
            return self.agent_id
        if self.user_id:
            return self.user_id
        return self.kind


__all__ = ["AgentRole", "Principal", "PrincipalKind"]
