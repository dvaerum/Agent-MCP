"""Per-tool authorization decorators (architecture review 2026-06-01,
candidate A; Principal-only since Wave 6 PR 6; capability-driven
since Wave 9 PR 1; bridge deleted by Wave 9 PR 6).

This module is the single, auditable surface for Agent-MCP tool
authorisation. Before retire-system-token Wave 6, every tool opened
with its own ``verify_token(...)`` block, returning a magic
``"Unauthorized: ..."`` ``TextContent``; the dispatcher then used
``_AUTH_FAILURE_RE`` to text-match those payloads back into an
exception so the MCP framework would set ``isError=True``. That left
the policy scattered across ~30 call sites, plus a regex that had to
stay in sync with each tool's exact wording.

The replacement is two decorators + one typed exception:

* :func:`requires_capability` wraps a tool entry point and raises
  :class:`AuthRejected` when the calling Principal (threaded through
  by ``dispatch_tool_call``) does not carry the requested capability
  (a member of :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`).

* :func:`requires_policy` is the toggle-gated variant for tools that
  admin can always call but workers can only reach when at least one
  listed ``config_*`` key in ``project_context`` evaluates truthy.

* :class:`AuthRejected` propagates through ``dispatch_tool_call`` to
  the MCP framework's ``_make_error_result`` (see
  ``mcp/server/lowlevel/server.py:584``) which sets ``isError=True``.
  No text matching, no regex.

Wave 6 PR 6 retired the ContextVar / ``verify_token`` plumbing the
decorators used to consult. The wrappers read the calling
:class:`agent_mcp.core.principal.Principal` from a keyword-only
``principal`` argument the dispatcher always supplies, and consult
``principal.has_capability(...)`` directly.

Wave 9 PR 6 deleted the legacy ``@requires`` / ``@requires_role``
decorators, the ``_check_role_principal`` legacy role-check, the
``_role_marker_cap`` bridge helper, and the ``_viewer_blocked``
defence-in-depth gate (operator-tier callers now go through
``has_capability`` directly; the viewer-vs-operator distinction is
encoded in :data:`PROJECT_ROLE_BUNDLES` — viewers don't carry the
write caps an operator-tier action requires). Capability gates are
now the single authorization surface.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Awaitable, Callable, Dict, Optional

from .principal import Principal
from .principal_builder import build_agent_bearer_principal
# arch-B: the operator-tier predicate is defined once in
# ``core.principal_builder``. Bound to the historical private name so the
# call site in :func:`requires_policy` (and the tests pinning it) keep
# working through the single shared definition.
from .principal_builder import is_operator_tier as _is_operator_tier


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
    """Raised by the @requires_capability / @requires_policy
    decorators when the caller's principal fails the configured
    policy.

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


def _synthesize_principal_from_arguments(
    arguments: Dict[str, Any],
) -> Optional[Principal]:
    """Build an ``agent_bearer`` Principal from a bearer in ``arguments``.

    Convenience fallback for direct in-process / unit-test calls into
    a ``@requires_capability`` / ``@requires_policy`` wrapper that
    don't supply ``principal=`` explicitly. The production path
    (``dispatch_tool_call``) always supplies one — this fallback only
    fires for tests / scripts that call the wrapped impl directly with
    just ``arguments``.

    Resolves the bearer via :func:`agent_mcp.core.auth.get_agent_id`
    and reads the row's ``agent_role`` from the in-memory cache when
    present, so the synthesized Principal carries the same
    discriminators the production seam would have produced.
    Returns None when no usable bearer is in hand; the wrapper then
    falls through to the cap reject path.

    arch-B: delegates to the shared
    :func:`agent_mcp.core.principal_builder.build_agent_bearer_principal`
    so a synthesized fallback Principal resolves its capabilities through
    the exact same path the seam uses — the fallback never diverges from
    the middleware-built identity.
    """
    raw_token = arguments.get("token")
    if not isinstance(raw_token, str) or not raw_token:
        return None
    return build_agent_bearer_principal(raw_token)


def requires_capability(cap: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a single capability.

    Wave 9 PR 0 — the single capability gate. ``cap`` must be a
    member of :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`;
    the decorator validates at construction time so a typo'd cap
    string fails at import rather than admitting silently at runtime.

    Single capability per decorator: a tool that needs two caps in a
    real either-or sense is a sign the cap vocabulary needs a coarser
    parent — the design decision (Wave 9 grilling 2026-06-30) is to
    keep one cap per decorator and surface in-body branching for the
    rare conditional case.

    Raises :class:`AuthRejected` on miss. Mirrors the bearer-
    synthesis fallback so direct in-process / unit-test calls that
    don't supply ``principal=`` keep working via ``arguments["token"]``.
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
        # cap X" without re-parsing the source.
        wrapper._required_capability = cap  # type: ignore[attr-defined]
        return wrapper

    return decorator


def requires_policy(
    *config_keys: str,
    default: bool,
) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point with the worker-toggle pattern.

    Operator-tier callers (cookie / forwarding-header / sysadmin)
    always pass. Worker tokens pass iff *at least one* of the listed
    ``config_keys`` resolves truthy in ``project_context``; the
    per-key default (used when the row is absent) is supplied here
    and must match what
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
            if _is_operator_tier(principal):
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
