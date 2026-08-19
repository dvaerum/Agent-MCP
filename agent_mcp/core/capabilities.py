"""Capability-based authorisation surface (Wave 9 PR 0 — foundation).

This module is the SINGLE source of truth for the capability vocabulary
that replaces the legacy role-tier model
(``operator``/``manager``/``admin``/``viewer``/``sysadmin``/``worker``)
during the Wave 9 migration window. See the Wave 9 section of
``/home/dennis/.claude/plans/prancy-napping-pie.md`` for the full
design context (locked 2026-06-30 via ``/grill-me``).

What lives here
---------------

* :data:`KNOWN_CAPABILITIES` — the exact 28-element frozenset of
  capability strings the system recognises. Any cap string used in
  decorators, in-body checks, or DB rows MUST be drawn from this set.
  Adding the 28th cap is a design change that re-opens the Wave 9
  bundle table.
* :data:`PROJECT_ROLE_BUNDLES` — caps granted to operator-tier callers
  by virtue of their ``project_membership.role`` (``"viewer"`` /
  ``"operator"``). Applied at :func:`resolve_capabilities` time to
  every operator-session / forwarding-header Principal that has a
  resolved project role.
* :data:`AGENT_ROLE_BUNDLES` — caps granted to agent-bearer callers by
  virtue of their ``agents.agent_role`` (``"worker"`` / ``"manager"``).
* :data:`SYSADMIN_WILDCARD` — sentinel cap string that
  :meth:`Principal.has_capability` short-circuits on. Stored exactly
  once per sysadmin Principal instead of expanding to every known cap;
  the wildcard semantics live in the method, not the resolver.
* :func:`resolve_capabilities` — the resolution function called at
  middleware time. Returns the frozenset attached to the per-request
  :class:`Principal`. Defensive against router-DB-not-initialised
  failures (returns the bundle-only subset so tests and bootstrap
  paths still produce a valid Principal).

Design constraints (locked):

* The cap vocabulary is per-resource × verb, AWS-IAM-style. Every
  string matches the regex ``^[a-z]+(\\.[a-z_]+)+$``.
* Bundles are subsets of :data:`KNOWN_CAPABILITIES` — the smoke test
  in ``tests/test_wave9_pr0_capabilities.py`` enforces this.
* Resource caps (non-``system.*``) require the calling Principal to
  have a project membership (``project_role is not None``) OR be an
  ``agent_bearer``. ``system.*`` caps are project-membership-ungated
  — they're router-admin verbs and don't belong to any one project.
* Sysadmin is encoded as ``frozenset({SYSADMIN_WILDCARD})``.
  :meth:`Principal.has_capability` returns True for any cap when the
  wildcard is present. This keeps the sysadmin path one constant-time
  check instead of materialising every known cap onto every sysadmin
  Principal.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .principal import AgentRole, PrincipalKind


logger = logging.getLogger(__name__)


# ── Capability vocabulary ───────────────────────────────────────────


#: The exact 28-element frozenset of capability strings the system
#: recognises. Locked by Wave 9 grilling (2026-06-30); adding /
#: removing entries is a design change that needs a new wave.
#:
#: NOTE (arch round-2 #6, 2026-07-11): a review flagged 8 of these
#: (agents.view, agents.use, memories.view, messages.view,
#: messages.send, coordination.wait, system.view, rag.rebuild) as
#: "checked by nothing" and candidate for pruning. They were KEPT:
#: every one is live, admin-assignable group-capability vocabulary
#: surfaced by the dashboard groups UI
#: (``dashboard/lib/capability-descriptions.ts``, bidirectionally
#: CI-coupled to this set) and validated by the group-capabilities
#: API (``router/admin_users_api.py``). Absence of a Python
#: enforcement gate does not make the vocabulary dead — an admin can
#: grant any of these to a group today.
KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    # MCP wire / agent operations
    "mcp.connect",              # fundamental gate to use MCP wire
    "agents.view",
    "agents.register",
    "agents.terminate",
    "agents.use",
    # Tasks
    "tasks.view",
    "tasks.create",
    "tasks.update",
    "tasks.delete",
    "tasks.assign",
    # Memories / project context
    "memories.view",
    "memories.create",
    "memories.update",
    "memories.delete",
    # Messages
    "messages.view",
    "messages.send",
    # Files / coordination / RAG
    "files.use",
    "coordination.assist",
    "coordination.wait",
    "rag.query",
    "rag.rebuild",
    # System management (router-side admin)
    "system.view",
    "system.config.write",
    "system.users.manage",
    "system.groups.manage",
    "system.groups.capabilities.manage",
    "system.projects.manage",
    "system.sso.configure",
})


# ── Bundle constants ────────────────────────────────────────────────


#: Caps granted to operator-tier callers based on
#: ``project_membership.role``. Resolved per-request at middleware
#: time. Viewer is read-only; operator is full write within the
#: project scope (still requires project membership — the resource
#: gate is what makes resource caps project-scoped).
PROJECT_ROLE_BUNDLES: dict[str, frozenset[str]] = {
    "viewer": frozenset({
        "agents.view",
        "tasks.view",
        "memories.view",
        "messages.view",
        "system.view",
    }),
    "operator": frozenset({
        # viewer (read-only baseline) +
        "agents.view",
        "tasks.view",
        "memories.view",
        "messages.view",
        "system.view",
        # write surfaces inside the project
        "agents.register",
        "agents.terminate",
        "tasks.create",
        "tasks.update",
        "tasks.delete",
        "tasks.assign",
        "memories.create",
        "memories.update",
        "memories.delete",
        "messages.send",
        "files.use",
        # operator-only system surfaces (per-project config + RAG)
        "system.config.write",
        "rag.query",
        "rag.rebuild",
    }),
}


#: Caps granted to agent-bearer callers based on
#: ``agents.agent_role``. Worker is the baseline; manager is worker +
#: supervisory verbs (task assignment + memory edit-over-others).
AGENT_ROLE_BUNDLES: dict[str, frozenset[str]] = {
    "worker": frozenset({
        "mcp.connect",
        "agents.use",
        "tasks.view",
        "tasks.create",
        "tasks.update",
        "memories.view",
        "messages.view",
        "messages.send",
        "files.use",
        "coordination.assist",
        "coordination.wait",
        "rag.query",
    }),
    "manager": frozenset({
        # worker (baseline) +
        "mcp.connect",
        "agents.use",
        "tasks.view",
        "tasks.create",
        "tasks.update",
        "memories.view",
        "messages.view",
        "messages.send",
        "files.use",
        "coordination.assist",
        "coordination.wait",
        "rag.query",
        # manager-tier additions
        "tasks.assign",
        "memories.update",
    }),
}


#: Sentinel cap string that :meth:`Principal.has_capability`
#: short-circuits on. A sysadmin Principal carries exactly
#: ``frozenset({SYSADMIN_WILDCARD})`` rather than the full 27-cap set;
#: the wildcard semantics live in the method, not the resolver.
SYSADMIN_WILDCARD: str = "*"


# ── Resolution ──────────────────────────────────────────────────────


def resolve_capabilities(
    *,
    user_id: Optional[str],
    agent_id: Optional[str],
    sysadmin: bool,
    agent_role: Optional["AgentRole"],
    project_role: Optional[str],
    kind: "PrincipalKind",
    groups: Optional[set[str]] = None,
) -> frozenset[str]:
    """Compute the capability set for a Principal at middleware time.

    Called exactly once per request at the outermost auth seam
    (``router/auth_middleware.py`` for the cookie path,
    ``app/main_app.py`` for the FastAPI bearer / forwarding-header
    path). The returned frozenset is attached to the per-request
    :class:`Principal` and never re-derived downstream.

    Resolution (per the Wave 9 design):

    * ``sysadmin=True`` → ``frozenset({SYSADMIN_WILDCARD})``. The
      wildcard short-circuit in ``has_capability`` admits every cap;
      we don't materialise the full 27-cap set onto every sysadmin
      Principal.
    * ``kind == "agent_bearer"`` → ``AGENT_ROLE_BUNDLES[agent_role]``
      verbatim (or empty when ``agent_role`` is absent / malformed).
      Group memberships don't apply to agent bearers — they're per-
      project tokens, not operator identities.
    * Otherwise (operator-session / forwarding-header) → union of
      ``PROJECT_ROLE_BUNDLES[project_role]`` (when set) and every cap
      attached to the user's transitively-resolved groups via
      :func:`group_capability_repository.fetch`. Group caps are
      additive; the bundle is the floor.

    Defensive against router-DB-not-initialised failures: the group
    lookup is wrapped in try/except so a test environment without
    router.db still produces the bundle-only subset rather than
    raising. The same defence lets the agent-side per-project backend
    (which has no router.db handle) call this safely.

    ``groups`` (arch-deepening R4 #3): pass the caller's already-
    resolved transitive group set to skip the internal
    ``resolve_user_groups`` walk — the request-scoped seam in
    ``router/auth_middleware.py`` resolves the graph once (for the
    sysadmin + project-role checks) and threads it through here too,
    so a single request pays for exactly one walk instead of a fourth.
    ``None`` (the default) self-resolves, preserving the original
    behaviour for every other caller (the FastAPI bearer /
    forwarding-header seam in ``app/main_app.py`` among them).
    """
    if sysadmin:
        return frozenset({SYSADMIN_WILDCARD})

    if kind == "agent_bearer":
        if agent_role is None:
            return frozenset()
        return AGENT_ROLE_BUNDLES.get(agent_role, frozenset())

    # operator_session / forwarding_header
    caps: set[str] = set()
    if user_id:
        try:
            from ..repositories.group_capability_repository import (
                fetch as _gcap_fetch,
            )
            if groups is None:
                from ..router.group_resolver import resolve_user_groups
                group_ids = resolve_user_groups(user_id)
            else:
                group_ids = groups
            for gid in group_ids:
                # SEC (Finding 2, 2026-07-08): defense-in-depth. Group
                # rows are data — a migration, repair script, second
                # writer, or direct SQL could land ``"*"`` (the sysadmin
                # wildcard) or a typo'd string here, and unioning it
                # verbatim would silently grant it. The wildcard must
                # ONLY ever be mintable by the ``sysadmin=True`` branch
                # above, never sourced from a group row; intersect with
                # KNOWN_CAPABILITIES (which excludes ``"*"``) so any
                # non-known string is dropped.
                #
                # SEC R2-F3 (2026-08-19): ``group_capability`` has no
                # ``project_name`` column — a grant is global to every
                # project the caller can reach, unlike
                # ``PROJECT_ROLE_BUNDLES`` (sourced from
                # ``project_membership.role``, which IS correctly
                # project-scoped via ``group_resolver``). Unioning a
                # resource-tier cap (e.g. ``memories.create``) from a
                # group row therefore lets an admin's grant intended
                # for project A silently authorise the SAME caller in
                # unrelated project B — a cross-project privilege
                # escalation confirmed live against the ``/mcp`` wire
                # (REST is separately gated by
                # ``require_operator_session_middleware``, but ``/mcp``
                # delegates authz entirely to each tool's bare
                # ``has_capability`` check). ``system.*`` caps have no
                # project dimension to violate — they're deployment-
                # wide router-admin verbs by design (see
                # ``Principal.has_capability``'s own ``system.``
                # short-circuit) — so only THOSE are admitted from a
                # group row. Resource-tier (non-``system.*``) caps must
                # flow only through the already project-scoped
                # ``PROJECT_ROLE_BUNDLES`` path below. The write side
                # (``admin_users_api.replace_group_capabilities_handler``)
                # mirrors this by rejecting non-``system.*`` grants to a
                # group outright, so this filter should never actually
                # need to drop a row in a healthy deploy — it's here as
                # the authoritative enforcement point regardless.
                caps |= {
                    cap for cap in (_gcap_fetch(gid) & KNOWN_CAPABILITIES)
                    if cap.startswith("system.")
                }
        except Exception:  # pragma: no cover - router.db not available
            # Tests / per-project backend / cold-start paths reach
            # here. The bundle (resolved below) is still applied; only
            # group-cap-overlay grants are skipped.
            pass
    if project_role:
        caps |= PROJECT_ROLE_BUNDLES.get(project_role, frozenset())
    return frozenset(caps)


__all__ = [
    "AGENT_ROLE_BUNDLES",
    "KNOWN_CAPABILITIES",
    "PROJECT_ROLE_BUNDLES",
    "SYSADMIN_WILDCARD",
    "resolve_capabilities",
]
