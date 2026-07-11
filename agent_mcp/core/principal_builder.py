"""One place to build a :class:`Principal` and resolve its capabilities.

Architecture-deepening candidate B ("build the Principal once, at one
seam"). Before this module, the ``agent_bearer`` construction block
(bearer → ``get_agent_id`` → normalized ``worker``/``manager`` role →
:func:`resolve_capabilities` → :class:`Principal`) was copy-pasted across
four sites — ``app/main_app.py``, ``app/_dispatch_helpers.py``,
``core/authorize.py``, ``tools/agent_communication_tools.py`` — and the
operator / forwarding-header block across three more. Each site
independently decided whether to call :func:`resolve_capabilities`
explicitly or lean on :meth:`Principal.__post_init__`'s lazy back-fill.

Consolidating construction here guarantees ONE capability-resolution
path: the same identity resolves to the same capability set no matter
which construction site fired. The seams (router
``auth_middleware.require_operator_session_middleware`` and backend
``AuthHeaderMiddleware``) call these builders directly; the downstream
REST dispatch helper and the two in-process fallback synthesizers call
the SAME builder so a synthesized identity gets the identical caps the
seam would have produced.

The operator-tier predicate (:func:`is_operator_tier`) lived in two
copies (``core/authorize._is_operator_tier`` and
``tools/agent_communication_tools._is_operator_tier``) that had already
DRIFTED — only the latter treated the legacy ``agent_id == "admin"``
label as operator-tier, so the same admin-labelled manager Principal was
classified differently by the two authorization surfaces. This module is
now the single definition; both call sites import it.
"""

from __future__ import annotations

from typing import Optional

from .principal import AgentRole, Principal, PrincipalKind


def normalize_agent_role(raw: object) -> Optional[AgentRole]:
    """Return ``raw`` iff it is a known agent role, else ``None``.

    The bearer path stores whatever ``agents.agent_role`` holds; only
    ``"worker"`` / ``"manager"`` are legitimate. Anything else (NULL, a
    stale/legacy value) collapses to ``None`` so
    :func:`resolve_capabilities` hands back an empty cap set rather than
    guessing.
    """
    return raw if raw in ("worker", "manager") else None  # type: ignore[return-value]


def build_agent_bearer_principal(
    bearer_token: Optional[str],
    *,
    resolve_wake_loop: bool = False,
) -> Optional[Principal]:
    """Build the ``agent_bearer`` Principal for ``bearer_token``.

    The single home for the block that was duplicated ×4: resolve the
    bearer to an ``agent_id`` (:func:`agent_mcp.core.auth.get_agent_id`),
    read + normalize the row's ``agent_role`` from the in-memory cache,
    resolve capabilities via :func:`resolve_capabilities`, and stamp the
    frozen Principal. Returns ``None`` when the bearer is absent or does
    not resolve to an agent — the caller surfaces that as its own
    "unauthenticated" outcome.

    ``resolve_wake_loop`` gates the wake-loop-eligibility DB lookup that
    only the MCP-wire seam (``main_app``) needs; every other caller
    leaves ``can_wake_loop`` at ``False`` (its historical value at those
    sites), so the flag defaults off.
    """
    if not bearer_token:
        return None
    from .auth import get_agent_id
    from . import globals as _g
    from .capabilities import resolve_capabilities

    agent_id = get_agent_id(bearer_token)
    if not agent_id:
        return None
    row = _g.active_agents.get(bearer_token) or {}
    agent_role = normalize_agent_role(row.get("agent_role"))
    can_wake_loop = (
        _resolve_can_wake_loop(agent_id) if resolve_wake_loop else False
    )
    caps = resolve_capabilities(
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        agent_role=agent_role,
        project_role=None,
        kind="agent_bearer",
    )
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=agent_role,
        can_wake_loop=can_wake_loop,
        source_token=bearer_token,
        capabilities=caps,
    )


def build_operator_principal(
    *,
    user_id: Optional[str],
    kind: PrincipalKind,
    project_role: Optional[str],
    sysadmin: bool,
    project_name: Optional[str] = None,
    source_token: Optional[str] = None,
) -> Principal:
    """Build an operator-tier Principal (cookie or forwarding-header).

    ``kind`` is ``"operator_session"`` (dashboard cookie / REST) or
    ``"forwarding_header"`` (signed router proxy). Capabilities are
    resolved once here via :func:`resolve_capabilities`, which unions the
    project-role bundle with any group-capability overlay the caller's
    ``user_id`` transitively resolves to (the overlay is a no-op wherever
    ``router.db`` is unavailable — e.g. the per-project backend — but the
    resolution path is identical, so no site can silently resolve a
    different set).
    """
    from .capabilities import resolve_capabilities

    caps = resolve_capabilities(
        user_id=user_id,
        agent_id=None,
        sysadmin=sysadmin,
        agent_role=None,
        project_role=project_role,
        kind=kind,
    )
    return Principal(
        kind=kind,
        user_id=user_id,
        agent_id=None,
        sysadmin=sysadmin,
        project_name=project_name,
        project_role=project_role,
        agent_role=None,
        can_wake_loop=False,
        source_token=source_token,
        capabilities=caps,
    )


def is_operator_tier(principal: Principal) -> bool:
    """True iff ``principal`` is an operator-tier caller.

    The single definition, collapsing the two that had drifted.
    Operator-tier = a caller carrying the per-project operator write
    marker (``system.config.write``, present in
    :data:`agent_mcp.core.capabilities.PROJECT_ROLE_BUNDLES["operator"]`
    and short-circuited by the sysadmin wildcard), OR the legacy
    ``agent_id == "admin"`` pseudo-agent label the test harness seeds
    (a manager-role row labelled ``admin``; production post-Wave-4 has no
    such row, so this collapses to the capability check there).

    A viewer-tier operator lacks the write marker and is excluded — the
    same viewer→operator collapse SEC1 (#273) closed on the wire.
    """
    return (
        principal.has_capability("system.config.write")
        or principal.agent_id == "admin"
    )


def _resolve_can_wake_loop(agent_id: str) -> bool:
    """Return the wake-loop eligibility for a bearer's ``agent_id``.

    Admin agents coordinate and don't run the worker wake loop; non-admin
    agents qualify when the global toggle is on AND their per-agent flag
    is on (default True). The per-agent flag is sourced from the DB rather
    than the in-memory cache so an operator who flipped it via REST in the
    current session sees the change on the next request. Any failure
    collapses to ``False`` (fail-closed on the bootstrap instruction).
    """
    if agent_id == "admin":
        return False
    try:
        from ..tools import access as _access
        from ..db.connection import get_db_connection

        if not _access._get_config_bool(
            "config_auto_event_loop_global", default=True
        ):
            return False
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT auto_event_loop FROM agents WHERE agent_id = ?",
                (agent_id,),
            )
            db_row = cursor.fetchone()
        finally:
            conn.close()
        return bool(db_row is not None and bool(db_row["auto_event_loop"]))
    except Exception:  # pragma: no cover - defensive
        return False


__all__ = [
    "build_agent_bearer_principal",
    "build_operator_principal",
    "is_operator_tier",
    "normalize_agent_role",
]
