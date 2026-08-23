"""``decide()`` — one authorization question, one answer, for the MCP
catalog surfaces (security-architecture hardening Phase 4, Finding E).

Why this exists
---------------

:class:`agent_mcp.core.registry.RegistryEntry` carries a ``visibility``
declaration that :meth:`Registry.list_visible` treats as authoritative
for ``*/list`` on all three catalogs (Prompts, Resources, Tools). At
VERB time the three surfaces then diverged:

* **Prompts** re-check it — :meth:`PromptRegistry.render` re-runs
  :func:`resolve_visibility` before rendering, because "``prompts/list``
  filtering alone is not enough if a worker guesses an admin-only id".
* **Resources** did not. ``resources/read`` ran a parallel gate
  (``catalog_role(principal) == "admin"`` for cross-agent reads, else
  "the URI's agent_id must equal your bearer's") that never consulted
  ``entry.visibility``. Benign only because both shipped resources are
  ``visibility="any"``: register one admin-only resource and
  ``resources/list`` hides it correctly while ``resources/read`` serves
  it to anyone who guesses the URI. That is the latent bug this module
  closes, and it is the same shape as R21-F4 — a read-path gate that
  disagrees with the list-path gate.
* **Tools** gate on the ``requires=`` stamp
  (:class:`agent_mcp.core.authorize.Cap` / ``Policy`` / ``Predicate`` /
  ``PUBLIC``) that ``dispatch_tool_call`` reads off the function object,
  which Phase 2 made a required registration argument. That mechanism is
  correct and is NOT superseded here; ``visibility=`` survives for tools
  only as a ``tools/list`` signal for ``@requires_predicate`` tools,
  which cannot derive a tier.

So: three surfaces, three answers to "may this caller do this?". This
module is the seam where that question gets asked once. Phase 4
deliberately migrates **Resources only** — Prompts and Router admin are
a documented follow-up, not part of this pass. :class:`Request` and
:class:`Decision` are shaped so those migrations are additive:

* Prompts would ask ``Request(principal, "prompts", "render", entry)``
  with no scope, and map a ``"not_visible"`` denial to the
  ``PermissionError`` its callers already expect.
* Tools would ask ``Request(principal, "tools", "call", entry)`` and map
  a denial to :class:`~agent_mcp.core.authorize.AuthRejected`. Their
  requirement vocabulary stays in ``core/authorize.py``: ``decide()``
  answers the *catalog* question (is this entry reachable by this
  caller, and is it addressed at a scope they own), not the per-tool
  capability question. The two compose; neither re-implements the other.

Design notes
------------

**The role always comes from**
:func:`agent_mcp.core.principal_builder.catalog_role` — the single
source of truth every catalog surface already filters on. ``decide()``
does not re-derive "is this caller an admin"; the admin branch of the
scoping gate and the ``role`` argument to
:func:`~agent_mcp.core.registry.resolve_visibility` are the SAME value,
so read-visibility can never drift from list-visibility per caller shape
(``tests/test_phase4_decide_seam.py`` pins that equivalence across every
Principal shape reachable in production: cookie-session operator,
forwarding-header proxy — both ``agent_id=None`` — the viewer-tier
forwarding caller, the legacy ``agent_id == "admin"`` bearer, a worker
bearer, and anonymous).

**Denials are classified, not phrased.** :class:`Decision` carries a
:data:`DenialKind` plus a generic diagnostic ``reason``; each surface
maps the kind onto its own error type and wire code (Resources →
:class:`ResourceReadError` with a JSON-RPC code, Prompts →
``PermissionError``, Tools → ``AuthRejected``). Keeping the user-facing
wording at the surface is what lets the seam stay free of MCP types and
lets existing messages survive a migration verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from .principal import Principal
from .registry import RegistryEntry, resolve_visibility

#: Which MCP catalog is asking. Only ``"resources"`` is wired today
#: (Phase 4's deliberate scope cut); the other two are listed so a later
#: migration is a call-site change, not a type change.
Surface = Literal["resources", "prompts", "tools"]

#: The verb being attempted. ``"list"`` is included for completeness —
#: ``Registry.list_visible`` already filters correctly and is not
#: migrated here — while ``"read"`` / ``"render"`` / ``"call"`` are the
#: per-entry verbs where the re-check matters.
Verb = Literal["list", "read", "render", "call"]

#: Why a request was refused. The surface maps this onto its own error
#: type and wire code; it is deliberately coarse (three reasons, not one
#: per message) so a caller can branch on it.
DenialKind = Literal["unauthenticated", "not_visible", "out_of_scope"]


@dataclass(frozen=True)
class Request:
    """One authorization question: may ``principal`` ``verb`` this entry?

    Attributes:
        principal: The caller, or ``None`` for an unauthenticated one.
            Mapped to a catalog role by
            :func:`~agent_mcp.core.principal_builder.catalog_role`.
        surface: Which catalog is asking. Informational today — the
            gates below are surface-agnostic — but carried so a denial
            can be attributed and so a surface-specific rule has an
            obvious home if one is ever needed.
        verb: What is being attempted.
        entry: The registry entry being addressed, when there is one.
            ``None`` means "no entry-level visibility question" (e.g. a
            surface asking only the scoping question); the visibility
            gate is then skipped rather than defaulting to deny.
        target_scope: The subject the request addresses. For Resources
            this is the ``agent_id`` embedded in the URI. ``None`` means
            the entry is not scoped to a subject (Prompts, Tools), and
            the scoping gate is skipped.
        caller_scope: The caller's own subject id, when the surface
            resolved it itself. Resources passes the bearer's
            ``agent_id`` — which it may have resolved via
            ``get_agent_id(token)`` rather than from the Principal.
            Falls back to ``principal.agent_id`` when omitted.
    """

    principal: Optional[Principal]
    surface: Surface
    verb: Verb
    entry: Optional[RegistryEntry[Any]] = None
    target_scope: Optional[str] = None
    caller_scope: Optional[str] = None

    def own_scope(self) -> Optional[str]:
        """The caller's subject id: explicit ``caller_scope`` first, else
        the Principal's ``agent_id``, else ``None``."""
        if self.caller_scope:
            return self.caller_scope
        return self.principal.agent_id if self.principal is not None else None


@dataclass(frozen=True)
class Decision:
    """The answer to a :class:`Request`.

    Truthy iff allowed, so ``if not decide(req):`` reads naturally, but
    the ``denial`` classification is what callers should branch on when
    they need to pick an error code.
    """

    allowed: bool
    denial: Optional[DenialKind] = None
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def allow(cls) -> "Decision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, denial: DenialKind, reason: str) -> "Decision":
        return cls(allowed=False, denial=denial, reason=reason)


def decide(request: Request) -> Decision:
    """Answer ``request``. Never raises; never performs I/O.

    Two gates, in order:

    1. **Visibility** — ``resolve_visibility(entry.visibility, role)``,
       the exact predicate :meth:`Registry.list_visible` filters on, with
       ``role`` from :func:`catalog_role`. An entry a caller cannot SEE
       in the catalog is one they cannot address by guessing its id.
       Skipped when the request carries no entry.
    2. **Scope** — an admin (per the same ``role``) may address any
       subject; anyone else may address only their own. Skipped when the
       request is not scoped to a subject.

    The admin check delegates to ``catalog_role`` rather than
    re-deriving operator-tier-ness, so it stays identical to the check
    ``resolve_agent_id_for_uri`` ran before this seam existed and to the
    one ``resources/list`` runs — Phase 4 is a mechanism change, not a
    policy change.
    """
    from .principal_builder import catalog_role

    role = catalog_role(request.principal)

    entry = request.entry
    if entry is not None and not resolve_visibility(entry.visibility, role):
        return Decision.deny(
            "not_visible",
            f"{request.surface} entry {entry.name!r} is not visible to "
            f"role {role!r}",
        )

    if request.target_scope is None:
        return Decision.allow()

    # Admin reads across subjects by design (operational visibility) —
    # and legitimately carries no subject id of its own, which is why
    # this branch must precede the own-scope resolution below (R21-F4).
    if role == "admin":
        return Decision.allow()

    own = request.own_scope()
    if not own:
        return Decision.deny(
            "unauthenticated",
            "caller does not resolve to a subject",
        )
    if own != request.target_scope:
        return Decision.deny(
            "out_of_scope",
            f"caller {own!r} may not address subject "
            f"{request.target_scope!r}",
        )
    return Decision.allow()


__all__ = [
    "Decision",
    "DenialKind",
    "Request",
    "Surface",
    "Verb",
    "decide",
]
