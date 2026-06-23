"""Per-tool authorization decorators (architecture review 2026-06-01,
candidate A).

This module is the single, auditable surface for Agent-MCP tool
authorisation. Before this PR, every tool in ``agent_mcp/tools/*.py``
opened with its own ``verify_token(...)`` block, returning a magic
``"Unauthorized: ..."`` ``TextContent``; the dispatcher then used
``_AUTH_FAILURE_RE`` to text-match those payloads back into an
exception so the MCP framework would set ``isError=True``. That left
the policy scattered across ~30 call sites, plus a regex that had to
stay in sync with each tool's exact wording.

The replacement is three decorators + one typed exception:

* :func:`requires` wraps a tool entry point and raises
  :class:`AuthRejected` when the supplied ``token`` (taken from
  ``arguments["token"]`` *and* the bearer-header fallback already
  injected by ``dispatch_tool_call`` — see Q6e in the plan) does not
  satisfy the requested role (``"admin"`` or ``"any"``).

* :func:`requires_policy` is the toggle-gated variant for tools that
  admin can always call but workers can only reach when at least one
  listed ``config_*`` key in ``project_context`` evaluates truthy. The
  per-key default — used when the row is absent — matches what each
  tool's own impl previously passed to ``_get_config_bool`` and is
  centralised in :data:`agent_mcp.tools.access._TOGGLE_DEFAULTS`.

* :class:`AuthRejected` propagates through ``dispatch_tool_call`` to
  the MCP framework's ``_make_error_result`` (see
  ``mcp/server/lowlevel/server.py:584``) which sets ``isError=True``.
  No text matching, no regex.

Tools that need ``is_admin`` for *branching* logic (e.g.
``view_project_context`` redacts secret keys for workers,
``update_task_status`` permits admins to edit fields workers can't)
keep their internal ``verify_token`` call. Only the *gating* call —
the leading "reject if not admin" — gets replaced by a decorator.
"""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Dict, List, Optional

import mcp.types as mcp_types

from .auth import verify_token, get_agent_id


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
# arguments → list of TextContent. Hoisting the alias keeps the
# decorator bodies readable.
ToolImpl = Callable[[Dict[str, Any]], Awaitable[List[mcp_types.TextContent]]]


def _extract_token(arguments: Dict[str, Any]) -> Optional[str]:
    """Best-effort token extraction.

    ``dispatch_tool_call`` already injects the ``Authorization: Bearer``
    header (when one is present) into ``arguments["token"]`` before
    handing off (Q6e fallback in ``tools/registry.py``), so by the
    time a decorator runs there is exactly one place to look.
    """
    raw = arguments.get("token")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    # Defensive: schema rejects non-strings, but a caller bypassing
    # the dispatcher (tests, in-process bridges) could still send a
    # weird type. Treat anything non-string as missing rather than
    # crashing on the eventual verify_token comparison.
    return None


#: Roles accepted by :func:`requires_role`. The legacy ``"admin"`` is a
#: deprecated alias for ``"operator"`` (kept for one release so existing
#: per-tool ``@requires("admin")`` decorators keep working until Wave 3
#: sweeps through with the new vocabulary).
_VALID_ROLES = frozenset({"operator", "manager", "any", "admin"})


def _check_role(role: str, token: Optional[str]) -> None:
    """Run the role gate. Raise :class:`AuthRejected` on rejection.

    Centralises the role → check mapping so :func:`requires` and
    :func:`requires_role` share one implementation. The function
    consults ``operator_session_active`` (set by the REST seam when
    the call originates from a logged-in operator's session cookie)
    in addition to the static token verification.

    Role semantics (Phase 2 Wave 2a, updated by retire-system-token
    Wave 1 — the system-bearer branch is gone; only operator-session
    and per-agent tokens admit):

    * ``"operator"`` (and legacy alias ``"admin"``) — admits operator
      session only. Agent tokens — including ``agent_role='manager'``
      — are rejected. This is the strictest gate; reserved for
      spawn/terminate-agent, mutate ``config_*``,
      ``broadcast_admin_message``, backup-context, and RAG-index
      rebuild.
    * ``"manager"`` — admits operator session OR agent token whose
      row has ``agent_role='manager'``. The supervision-tier gate:
      assign-task to peers, edit subordinate agent metadata.
    * ``"any"`` — any active agent token (worker or manager).
      Operator session does NOT satisfy ``"any"`` on its own because
      ``"any"`` is about agent-side identity (audit-log attribution
      needs an agent_id); operator-session callers that need to
      invoke an ``"any"``-gated tool must explicitly pass a per-agent
      token in ``arguments["token"]``.
    """
    # Lazy import — avoid an import cycle (registry imports authorize
    # transitively via tool implementations' @requires decorators).
    from ..tools.registry import (
        operator_session_active,
        operator_user_id,
        operator_project_name,
    )

    op_session = bool(operator_session_active.get())

    # Phase 3 Wave 2 (v5.0.69): when the REST seam stamps an
    # operator's user_id + the targeted project on the contextvars,
    # consult ``resolve_user_project_role`` so a viewer-tier
    # operator can't reach an ``"operator"``-gated tool. This is the
    # defence-in-depth gate; the router's
    # ``require_operator_session_middleware`` is the primary one
    # (rejects viewer mutations before they reach the backend), but
    # the decorator stays as a backstop for in-process callers that
    # bypass the REST seam.
    def _viewer_blocked() -> bool:
        if not op_session:
            return False
        uid = operator_user_id.get()
        proj = operator_project_name.get()
        if not uid or not proj:
            return False
        try:
            from ..router import group_resolver
            # Sysadmin always admits regardless of project role.
            if group_resolver.resolve_user_is_sysadmin(uid):
                return False
            resolved = group_resolver.resolve_user_project_role(uid, proj)
        except Exception:  # pragma: no cover - defensive
            # Resolver failure → don't double-restrict; the
            # middleware already gated the call.
            return False
        # No row → no membership; fall through to "blocked" because
        # the static role gates below would otherwise admit on
        # op_session alone. The middleware would have rejected this
        # case already; defence in depth.
        if resolved is None:
            return True
        return resolved == "viewer"

    if role in ("operator", "admin"):
        # Operator session is sufficient — no token needed (the REST
        # seam still passes the system token, but a hypothetical
        # operator-only path that doesn't could authorise here).
        if op_session:
            if _viewer_blocked():
                raise AuthRejected(
                    "Unauthorized: viewer-tier operator cannot perform "
                    "this operator-only action"
                )
            return
        # System bearer is sufficient (legacy admin scripts, the
        # REST seam's standard path).
        if verify_token(token, "system"):
            return
        raise AuthRejected("Unauthorized: Operator session or system token required")

    if role == "manager":
        if op_session:
            if _viewer_blocked():
                raise AuthRejected(
                    "Unauthorized: viewer-tier operator cannot perform "
                    "this manager-or-above action"
                )
            return
        # Manager-tier check accepts the system bearer OR an agent
        # whose row has agent_role='manager'.
        if verify_token(token, "manager"):
            return
        raise AuthRejected("Unauthorized: Manager role or operator session required")

    if role == "any":
        if not get_agent_id(token):
            raise AuthRejected("Unauthorized: Valid token required")
        return

    raise ValueError(  # pragma: no cover — guarded at decorator construction
        f"_check_role: unknown role {role!r}"
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
        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
        ) -> List[mcp_types.TextContent]:
            token = _extract_token(arguments)
            _check_role(role, token)
            return await func(arguments)

        # PR-W1c (2026-06-05): expose the role on the wrapper for the
        # derived `agent_mcp.tools.access.TOOL_ACCESS` map. The
        # `requires_role` alias in `agent_mcp/tools/_access.py` sets
        # the same attribute; tagging it here keeps the existing
        # `@requires("admin")` call sites discoverable without
        # forcing every tool module to re-decorate.
        wrapper._required_role = role  # type: ignore[attr-defined]
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
        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
        ) -> List[mcp_types.TextContent]:
            token = _extract_token(arguments)

            # Admin path: always permitted, no toggle read needed.
            # retire-system-token Wave 1: ``verify_token(.., "admin")``
            # now consults the operator-session ContextVar (set by
            # the REST seam / forwarding-header middleware). Fall back
            # to the agent-id-is-"admin" label so an admin-row token
            # arriving via the bearer-only MCP path also takes the
            # admin branch (the harness's admin-row token IS the
            # post-Wave-1 admin bearer surface).
            if verify_token(token, "admin"):
                return await func(arguments)

            # Worker path: must resolve to an active agent first.
            caller_agent_id = get_agent_id(token)
            if not caller_agent_id:
                raise AuthRejected("Unauthorized: Valid token required")
            if caller_agent_id == "admin":
                return await func(arguments)

            # Lazy import: the access module pulls in DB helpers we
            # don't want to load at module-import time (keeps
            # decorator import cheap for code paths that never run a
            # real tool — e.g. some unit tests).
            from ..tools.access import _get_config_bool

            for key in config_keys:
                if _get_config_bool(key, default):
                    return await func(arguments)

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
