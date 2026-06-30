"""Per-tool authorization decorators (architecture review 2026-06-01,
candidate A; Principal-only since Wave 6 PR 6; capability-driven
since Wave 9 PR 1).

This module is the single, auditable surface for Agent-MCP tool
authorisation. Before retire-system-token Wave 6, every tool opened
with its own ``verify_token(...)`` block, returning a magic
``"Unauthorized: ..."`` ``TextContent``; the dispatcher then used
``_AUTH_FAILURE_RE`` to text-match those payloads back into an
exception so the MCP framework would set ``isError=True``. That left
the policy scattered across ~30 call sites, plus a regex that had to
stay in sync with each tool's exact wording.

The replacement is three decorators + one typed exception:

* :func:`requires` wraps a tool entry point and raises
  :class:`AuthRejected` when the calling Principal (threaded through
  by ``dispatch_tool_call``) does not satisfy the requested role
  (``"admin"`` or ``"any"``).

* :func:`requires_policy` is the toggle-gated variant for tools that
  admin can always call but workers can only reach when at least one
  listed ``config_*`` key in ``project_context`` evaluates truthy.

* :class:`AuthRejected` propagates through ``dispatch_tool_call`` to
  the MCP framework's ``_make_error_result`` (see
  ``mcp/server/lowlevel/server.py:584``) which sets ``isError=True``.
  No text matching, no regex.

Wave 6 PR 6 retired the ContextVar / ``verify_token`` plumbing the
decorators used to consult. The wrappers now read the calling
:class:`agent_mcp.core.principal.Principal` from a keyword-only
``principal`` argument the dispatcher always supplies, and consult
``principal.has_capability(...)`` / ``principal.kind`` / sysadmin
flags directly.

Wave 9 PR 1 migrated the four ``has_role(...)`` / ``kind``
admit-checks inside :func:`_check_role_principal` to
``has_capability(...)`` via the :func:`_role_marker_cap` helper. Each
legacy role string admits when the principal carries a single
*marker cap* that uniquely identifies the tier —
``system.config.write`` for operator/admin, ``tasks.assign`` for
manager, ``mcp.connect`` for any. The legacy operator-tier widening
on the ``"any"`` branch (cookie-session / forwarding-header callers
admit even though their bundles don't grant ``mcp.connect``) is
preserved via a kind-based fallback, matching the pre-Wave-9
external contract. The ``has_role`` bridge stays alive on the
Principal until Wave 9 PR 6 deletes it alongside the deprecated
``requires`` / ``requires_role`` decorators.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional

import mcp.types as mcp_types

from .principal import Principal


def _func_accepts_principal(func: Callable) -> bool:
    """True iff ``func`` declares a ``principal`` keyword parameter.

    Inspected once per decorator construction so the wrapper can skip
    passing ``principal=`` to legacy / test-fixture impls that don't
    take it. ``functools.wraps`` keeps ``inspect.signature`` reading
    through to the underlying impl.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    return "principal" in sig.parameters


class AuthRejected(Exception):
    """Raised by the @requires/@requires_policy decorators when the
    caller's token fails the configured policy.

    Carries a short, user-facing ``reason`` that the MCP framework's
    error path (``_make_error_result``) surfaces verbatim to clients
    as the ``isError=True`` payload. Keep reasons concise — they end
    up in agent transcripts.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Tool entry points all share this signature: a single dict of
# arguments → typed ToolResult variant. Hoisting the alias keeps the
# decorator bodies readable.
ToolImpl = Callable[..., Awaitable[Any]]


#: Roles accepted by :func:`requires_role`. The legacy ``"admin"`` is a
#: deprecated alias for ``"operator"`` (kept for one release so existing
#: per-tool ``@requires("admin")`` decorators keep working until Wave 3
#: sweeps through with the new vocabulary).
_VALID_ROLES = frozenset({"operator", "manager", "any", "admin"})


def _synthesize_principal_from_arguments(
    arguments: Dict[str, Any],
) -> Optional[Principal]:
    """Build an ``agent_bearer`` Principal from a bearer in ``arguments``.

    Convenience fallback for direct in-process / unit-test calls into
    a ``@requires`` / ``@requires_role`` / ``@requires_policy`` wrapper
    that don't supply ``principal=`` explicitly. The production path
    (``dispatch_tool_call``) always supplies one — this fallback only
    fires for tests / scripts that call the wrapped impl directly with
    just ``arguments``.

    Resolves the bearer via :func:`agent_mcp.core.auth.get_agent_id`
    and reads the row's ``agent_role`` from the in-memory cache when
    present, so the synthesized Principal carries the same
    discriminators the production seam would have produced.
    Returns None when no usable bearer is in hand; the wrapper then
    falls through to the role's reject path.
    """
    raw_token = arguments.get("token")
    if not isinstance(raw_token, str) or not raw_token:
        return None
    from .auth import get_agent_id
    from . import globals as _g
    agent_id = get_agent_id(raw_token)
    if not agent_id:
        return None
    row = _g.active_agents.get(raw_token) or {}
    agent_role = row.get("agent_role")
    normalized_role = (
        agent_role if agent_role in ("worker", "manager") else None
    )
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=normalized_role,
        can_wake_loop=False,
        source_token=raw_token,
    )


def _viewer_blocked(principal: Principal) -> bool:
    """Defence-in-depth viewer gate for operator-tier decorators.

    Phase 3 Wave 2: a viewer-tier operator (read-only project member)
    must NOT bypass an ``@requires_role("operator")`` /
    ``@requires_role("manager")`` decorator just because they hold a
    valid session cookie. The router middleware
    (``require_operator_session_middleware``) is the primary gate —
    it 403s viewer mutations before they reach the per-project
    backend — but the decorator backstops in-process call sites that
    bypass the REST seam (tests, batch jobs).

    Returns True iff the principal is an operator-path caller with a
    resolved project membership of ``"viewer"``. Sysadmins are
    exempt. ``agent_bearer`` principals never have a project_role and
    fall through this check (False).
    """
    if principal.kind not in ("operator_session", "forwarding_header"):
        return False
    if principal.sysadmin:
        return False
    # Prefer the Principal's own project_role when set (the REST seam
    # / dashboard handlers fill it from ``resolve_user_project_role``).
    if principal.project_role == "viewer":
        return True
    # Forwarding-header path doesn't carry project_role yet — the
    # router-side middleware filled it on its own auth surface and
    # the per-project backend has no router.db handle. Re-resolve
    # here so the decorator's defence-in-depth still works for
    # cookie-only forwards without an explicit project_role.
    if (
        principal.kind == "forwarding_header"
        and principal.user_id
        and principal.project_name
        and principal.project_role is None
    ):
        try:
            from ..router import group_resolver
            if group_resolver.resolve_user_is_sysadmin(principal.user_id):
                return False
            resolved = group_resolver.resolve_user_project_role(
                principal.user_id, principal.project_name,
            )
        except Exception:  # pragma: no cover - defensive
            return False
        if resolved is None:
            return True
        return resolved == "viewer"
    return False


def _role_marker_cap(role: str) -> str:
    """Return the single capability whose presence asserts the role tier.

    Wave 9 PR 1 — collapses :func:`_check_role_principal`'s legacy
    role-name dispatch into a one-line capability lookup. Each marker
    cap is the cap that uniquely identifies the tier in the bundle
    table at :mod:`agent_mcp.core.capabilities`:

    * ``"operator"`` / ``"admin"`` → ``"system.config.write"``.
      The cap appears only in
      ``PROJECT_ROLE_BUNDLES["operator"]`` — NOT in viewer, NOT in
      any agent bundle — so it cleanly distinguishes operator-tier
      from every other admit. Sysadmins admit via the
      ``has_capability`` wildcard short-circuit.
    * ``"manager"`` → ``"tasks.assign"``. The cap appears in both
      ``PROJECT_ROLE_BUNDLES["operator"]`` and
      ``AGENT_ROLE_BUNDLES["manager"]``, encoding the legacy
      "operator-tier OR manager-role agent" contract in a single
      cap.
    * ``"any"`` → ``"mcp.connect"``. Baseline cap every
      ``agent_bearer`` carries via ``AGENT_ROLE_BUNDLES``;
      sysadmins still admit via the wildcard. Operator-tier
      non-sysadmin callers are admitted via a kind-based fallback
      in :func:`_check_role_principal` (the bundle intentionally
      doesn't grant ``mcp.connect`` to operator-tier — that's the
      MCP-wire baseline, not a REST admit — so the fallback
      preserves the pre-Wave-9 contract documented in the old
      "any" inline comment).

    Wave 9 PR 6 deletes this helper alongside the legacy
    ``"operator"`` / ``"manager"`` / ``"any"`` vocabulary itself
    (every call site moves to ``@requires_capability("<cap>")``
    directly).
    """
    if role in ("operator", "admin"):
        return "system.config.write"
    if role == "manager":
        return "tasks.assign"
    if role == "any":
        return "mcp.connect"
    raise ValueError(  # pragma: no cover — guarded at decorator construction
        f"_role_marker_cap: unknown role {role!r}"
    )


def _check_role_principal(role: str, principal: Principal) -> None:
    """Run the role gate against the typed Principal.

    Raise :class:`AuthRejected` on rejection.

    Wave 9 PR 1 migrated the four legacy role-tier admit-checks
    (three ``has_role(...)`` calls + the ``"any"`` branch's
    ``kind == "agent_bearer"`` first check) to
    ``has_capability(...)`` via the :func:`_role_marker_cap` helper.
    External contract (admit/reject decision for ``"operator"`` /
    ``"manager"`` / ``"any"`` role strings) is preserved — the only
    change is the internal check switching from role-name bridge to
    direct capability lookup, with a kind-based fallback on the
    ``"any"`` branch to preserve the pre-Wave-9 operator-tier
    widening.

    Role semantics:

    * ``"operator"`` (legacy alias ``"admin"``) — admits any
      operator-tier caller (cookie-session, forwarding-header, or
      sysadmin) that carries ``system.config.write``. Agent tokens —
      including ``agent_role='manager'`` — are rejected (no agent
      bundle grants the operator-only marker cap). Reserved for
      spawn/terminate-agent, mutate ``config_*``,
      broadcast-admin-message, backup-context, RAG rebuild.
    * ``"manager"`` — admits operator-tier OR agents whose row has
      ``agent_role='manager'`` (both bundles include
      ``tasks.assign``).
    * ``"any"`` — admits any ``agent_bearer`` (worker or manager via
      ``mcp.connect``) OR any operator-tier caller (kind-based
      fallback; the bundle deliberately doesn't grant
      ``mcp.connect`` to operator-tier so the fallback preserves the
      pre-Wave-9 admit semantics for the cookie / forwarding-header
      paths). The gate is about "an active caller identity" because
      audit-log attribution needs an agent_id (or operator user_id).
    """
    if role in ("operator", "admin"):
        if principal.has_capability(_role_marker_cap(role)):
            if _viewer_blocked(principal):
                raise AuthRejected(
                    "Unauthorized: viewer-tier operator cannot perform "
                    "this operator-only action"
                )
            return
        raise AuthRejected(
            "Unauthorized: Operator session or system token required"
        )

    if role == "manager":
        if principal.has_capability(_role_marker_cap(role)):
            if _viewer_blocked(principal):
                raise AuthRejected(
                    "Unauthorized: viewer-tier operator cannot perform "
                    "this manager-or-above action"
                )
            return
        raise AuthRejected(
            "Unauthorized: Manager role or operator session required"
        )

    if role == "any":
        if principal.has_capability(_role_marker_cap(role)):
            return
        # Operator-tier callers (cookie-session, forwarding-header,
        # sysadmin) admit too: the legacy contract was "any active
        # caller identity"; the typed Principal's operator path is
        # an identity the gate had no way to express pre-Wave-6.
        # The bundles deliberately don't grant ``mcp.connect`` to
        # operator-tier (the cap is the MCP-wire baseline, not a
        # REST admit), so a kind-based fallback preserves the
        # pre-Wave-9 contract that a cap-only check would tighten.
        if principal.kind in ("operator_session", "forwarding_header"):
            return
        raise AuthRejected("Unauthorized: Valid token required")

    raise ValueError(  # pragma: no cover — guarded at decorator construction
        f"_check_role_principal: unknown role {role!r}"
    )


def requires(role: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a static role.

    Backwards-compat thin wrapper around :func:`requires_role`. New
    code should call ``requires_role`` directly (it accepts the full
    vocabulary; ``requires`` is kept for one release so the existing
    ``@requires("admin")`` decorators continue to work unmodified).

    ``role`` is one of ``"admin"`` (deprecated alias for ``"operator"``)
    or ``"any"`` — the same vocabulary the pre-Wave-2a decorator
    accepted. Use :func:`requires_role` for the new ``"manager"`` and
    ``"operator"`` gates.

    Raises :class:`AuthRejected` on miss.
    """
    if role not in ("admin", "any"):
        raise ValueError(
            f"@requires(role={role!r}) — role must be 'admin' or 'any'; "
            "use @requires_role for 'operator' / 'manager'"
        )
    return requires_role(role)


def requires_role(role: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a Phase 2 Wave 2a role.

    See :func:`_check_role` for the per-role admission matrix.

    ``role`` is one of ``"operator"``, ``"manager"``, ``"any"``, or
    the legacy alias ``"admin"`` (== ``"operator"`` for backwards
    compat). The wrapper exposes ``_required_role`` so
    :func:`agent_mcp.tools.access._derive_access_level` can build the
    tools/list visibility map without re-parsing the source.

    Raises :class:`AuthRejected` on miss.
    """
    if role not in _VALID_ROLES:
        raise ValueError(
            f"@requires_role(role={role!r}) — role must be one of "
            f"{sorted(_VALID_ROLES)}"
        )

    def decorator(func: ToolImpl) -> ToolImpl:
        forward_principal = _func_accepts_principal(func)

        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
            *,
            principal: Optional[Principal] = None,
            **kwargs: Any,
        ) -> Any:
            # Wave 6 PR 6: the dispatcher always supplies ``principal``
            # — but direct in-process / unit-test calls may not. Fall
            # back to synthesizing one from ``arguments["token"]`` so
            # the wrapper stays usable as a callable in isolation.
            if principal is None:
                principal = _synthesize_principal_from_arguments(arguments)
            if principal is None:
                raise AuthRejected("Unauthorized: Valid token required")
            _check_role_principal(role, principal)
            if forward_principal:
                return await func(arguments, principal=principal, **kwargs)
            return await func(arguments, **kwargs)

        # PR-W1c (2026-06-05): expose the role on the wrapper for the
        # derived `agent_mcp.tools.access.TOOL_ACCESS` map. The
        # `requires_role` alias in `agent_mcp/tools/_access.py` sets
        # the same attribute; tagging it here keeps the existing
        # `@requires("admin")` call sites discoverable without
        # forcing every tool module to re-decorate.
        wrapper._required_role = role  # type: ignore[attr-defined]
        return wrapper

    return decorator


def requires_capability(cap: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a single capability.

    Wave 9 PR 0 — new decorator alongside the existing
    :func:`requires` / :func:`requires_role`. The capability vocabulary
    (see :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`)
    replaces the legacy role tiers; PRs 1-5 migrate every existing
    ``@requires(...)`` / ``@requires_role(...)`` call site to this
    decorator. PR 6 deletes the deprecated decorators once the
    migration completes.

    Single capability per decorator: a tool that needs two caps in a
    real either-or sense is a sign the cap vocabulary needs a coarser
    parent — the design decision (Wave 9 grilling 2026-06-30) is to
    keep one cap per decorator and surface in-body branching for the
    rare conditional case. ``cap`` must be a member of
    :data:`KNOWN_CAPABILITIES`; the decorator validates at
    construction time so a typo'd cap string fails at import rather
    than admitting silently at runtime.

    Raises :class:`AuthRejected` on miss. Mirrors the bearer-
    synthesis fallback from :func:`requires_role` so direct in-process
    / unit-test calls that don't supply ``principal=`` keep working
    via ``arguments["token"]``.
    """
    from .capabilities import KNOWN_CAPABILITIES

    if cap not in KNOWN_CAPABILITIES:
        raise ValueError(
            f"@requires_capability(cap={cap!r}) — cap must be a member of "
            f"agent_mcp.core.capabilities.KNOWN_CAPABILITIES"
        )

    def decorator(func: ToolImpl) -> ToolImpl:
        forward_principal = _func_accepts_principal(func)

        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
            *,
            principal: Optional[Principal] = None,
            **kwargs: Any,
        ) -> Any:
            if principal is None:
                principal = _synthesize_principal_from_arguments(arguments)
            if principal is None:
                raise AuthRejected("Unauthorized: Valid token required")
            if not principal.has_capability(cap):
                raise AuthRejected(
                    f"Unauthorized: capability {cap!r} required"
                )
            if forward_principal:
                return await func(arguments, principal=principal, **kwargs)
            return await func(arguments, **kwargs)

        # Expose the cap on the wrapper so the visibility map in
        # ``agent_mcp.tools.access`` can rebuild "this tool requires
        # cap X" without re-parsing the source. Mirrors the
        # ``_required_role`` attribute requires_role sets.
        wrapper._required_capability = cap  # type: ignore[attr-defined]
        return wrapper

    return decorator


def requires_policy(
    *config_keys: str,
    default: bool,
) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point with the worker-toggle pattern.

    Admin tokens always pass. Worker tokens pass iff *at least one*
    of the listed ``config_keys`` resolves truthy in
    ``project_context``; the per-key default (used when the row is
    absent) is supplied here and must match what
    :data:`agent_mcp.tools.access._TOGGLE_DEFAULTS` declares.

    Any-key (rather than all-key) semantics mirror
    :func:`agent_mcp.tools.access.is_visible_to_role`: if the worker
    can do *anything* with the tool under the current toggles, the
    tool is callable. The per-call enforcement of which Mode they can
    use stays inside the impl (e.g. ``assign_task``'s
    ``_authorize_assign_task`` distinguishes self-claim from
    file-unassigned).
    """
    if not config_keys:
        raise ValueError(
            "@requires_policy must list at least one config_* key"
        )

    def decorator(func: ToolImpl) -> ToolImpl:
        forward_principal = _func_accepts_principal(func)

        async def _call(arguments: Dict[str, Any], principal: Principal, kwargs: Dict[str, Any]):
            if forward_principal:
                return await func(arguments, principal=principal, **kwargs)
            return await func(arguments, **kwargs)

        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
            *,
            principal: Optional[Principal] = None,
            **kwargs: Any,
        ) -> Any:
            # Wave 6 PR 6: dispatcher supplies principal; tests that
            # call the wrapped impl directly may not — fall back to
            # synthesizing one from ``arguments["token"]``.
            if principal is None:
                principal = _synthesize_principal_from_arguments(arguments)
            if principal is None:
                raise AuthRejected("Unauthorized: Valid token required")
            # Operator-tier callers (and the harness's
            # ``agent_id == "admin"`` label that historically stood in
            # for "operator at the dashboard") bypass the toggle check.
            if principal.has_role("admin"):
                return await _call(arguments, principal, kwargs)

            # Agent path: a worker / manager bearer is required.
            if principal.kind != "agent_bearer" or not principal.agent_id:
                raise AuthRejected("Unauthorized: Valid token required")
            if principal.agent_id == "admin":
                return await _call(arguments, principal, kwargs)

            # Lazy import: the access module pulls in DB helpers we
            # don't want to load at module-import time (keeps
            # decorator import cheap for code paths that never run a
            # real tool — e.g. some unit tests).
            from ..tools.access import _get_config_bool

            for key in config_keys:
                if _get_config_bool(key, default):
                    return await _call(arguments, principal, kwargs)

            joined = ", ".join(config_keys)
            raise AuthRejected(
                f"Unauthorized: worker access denied by project policy "
                f"(all of: {joined} are off). Ask admin to enable "
                "one in dashboard Settings."
            )

        # PR-W1c (2026-06-05): expose the toggle keys + default on the
        # wrapper so the derived TOOL_ACCESS map can rebuild the
        # `worker-if-toggled:<keys>` access level string without
        # re-parsing the source. The level string keeps any-of
        # semantics (matches `is_visible_to_role`).
        wrapper._required_policy_keys = tuple(config_keys)  # type: ignore[attr-defined]
        wrapper._required_policy_default = default  # type: ignore[attr-defined]
        return wrapper

    return decorator
