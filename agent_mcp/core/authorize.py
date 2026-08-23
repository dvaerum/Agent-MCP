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

    token-retirement PR 2 (Phase B): the bearer is sourced from the
    ``request_auth_token`` ContextVar — the same seam the HTTP
    middleware (``main_app.AuthHeaderMiddleware``), the REST dispatch
    helper, and the test harness all set — NOT from a token argument.
    Production always threads an explicit ``principal=`` so this
    fallback only fires for direct in-process / unit-test calls; those
    make the bearer visible via the ContextVar (e.g.
    ``tests.harness.with_bearer``). ``arguments`` is retained in the
    signature (callers still pass it) but no longer read for the token
    — nothing reads a token argument for identity here.

    arch-B: delegates to the shared
    :func:`agent_mcp.core.principal_builder.build_agent_bearer_principal`
    so a synthesized fallback Principal resolves its capabilities through
    the exact same path the seam uses — the fallback never diverges from
    the middleware-built identity.
    """
    # Lazy import: ``tools.registry`` imports ``core.authorize`` (for
    # the dispatcher's principal-synthesis fallback), so a module-level
    # import here would close an import cycle.
    from ..tools.registry import request_auth_token

    raw_token = request_auth_token.get()
    if not isinstance(raw_token, str) or not raw_token:
        return None
    return build_agent_bearer_principal(raw_token)


def check_capability_gate(
    principal: Optional[Principal],
    cap: str,
    reason: Optional[str] = None,
) -> None:
    """Raise :class:`AuthRejected` iff ``principal`` lacks ``cap``.

    ``reason`` overrides the generic ``"Unauthorized: capability 'x'
    required"`` text for the cap-missing branch. Phase 2 (Finding A):
    several tools whose gate IS a single capability had hand-written
    in-body denials carrying actionable worker guidance ("this is an
    operator-only action; ask a project operator; you can still read
    with view_file_metadata"), pinned by
    ``tests/test_worker_msg_file_tools_clarity.py``. Without this
    override, moving such a gate to the decorator would either lose
    that message or push the author to reach for
    :func:`requires_predicate` purely for its custom ``reason`` — using
    the wrong gate shape for a single-cap check. The DECISION is
    unaffected either way; only the wording is.

    R20-F4: the single evaluation of "does this principal carry this
    capability", shared by the :func:`requires_capability` wrapper AND
    ``dispatch_tool_call``'s pre-schema-validation gate
    (``agent_mcp.tools.registry``). Extracted so the dispatcher can
    run the SAME check the decorator would have run — before
    ``jsonschema.validate`` — without a second, potentially-drifting
    copy of the capability-resolution logic. Reads the tool's
    ``_required_capability`` attribute that ``requires_capability``
    stamps on its wrapper (the same attribute
    ``agent_mcp.tools.access._derive_access_level`` already consults
    for ``tools/list`` visibility).
    """
    if principal is None:
        raise AuthRejected(reason or "Unauthorized: Valid token required")
    if not principal.has_capability(cap):
        raise AuthRejected(
            reason or f"Unauthorized: capability {cap!r} required"
        )


def requires_capability(
    cap: str, *, reason: Optional[str] = None
) -> Callable[[ToolImpl], ToolImpl]:
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
    don't supply ``principal=`` keep working via the
    ``request_auth_token`` ContextVar.

    ``reason`` (keyword-only) replaces the generic denial text with a
    tool-specific, actionable one — see :func:`check_capability_gate`.
    Use it when the tool already had a hand-written worker-facing
    message worth keeping; it does not change who is admitted.
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
            check_capability_gate(principal, cap, reason)
            if forward_principal:
                return await func(arguments, principal=principal, **kwargs)
            return await func(arguments, **kwargs)

        # Expose the cap on the wrapper so the visibility map in
        # ``agent_mcp.tools.access`` can rebuild "this tool requires
        # cap X" without re-parsing the source. The reason rides along
        # so ``dispatch_tool_call``'s pre-schema gate (which re-runs
        # ``check_capability_gate`` itself) produces the SAME message
        # this wrapper would have — otherwise the denial text would
        # depend on which of the two gates fired first.
        wrapper._required_capability = cap  # type: ignore[attr-defined]
        wrapper._required_capability_reason = reason  # type: ignore[attr-defined]
        return wrapper

    return decorator


def check_policy_gate(
    principal: Optional[Principal],
    config_keys: tuple,
    default: Optional[bool],
) -> None:
    """Raise :class:`AuthRejected` iff ``principal`` fails the
    toggle-gated worker-access policy for ``config_keys``.

    R20-F4: the single evaluation of the ``@requires_policy`` policy,
    shared by the decorator's wrapper AND ``dispatch_tool_call``'s
    pre-schema-validation gate — see :func:`check_capability_gate` for
    the same rationale on the capability side. Reads the tool's
    ``_required_policy_keys`` / ``_required_policy_default`` attributes
    that :func:`requires_policy` stamps on its wrapper.
    """
    if principal is None:
        raise AuthRejected("Unauthorized: Valid token required")
    # Operator-tier callers (and the harness's ``agent_id == "admin"``
    # label that historically stood in for "operator at the dashboard")
    # bypass the toggle check.
    if _is_operator_tier(principal):
        return

    # Agent path: a worker / manager bearer is required.
    if principal.kind != "agent_bearer" or not principal.agent_id:
        raise AuthRejected("Unauthorized: Valid token required")

    # Lazy import: the access module pulls in DB helpers we don't want
    # to load at module-import time (keeps decorator import cheap for
    # code paths that never run a real tool — e.g. some unit tests).
    from ..tools.access import _get_config_bool

    for key in config_keys:
        if _get_config_bool(key, default):
            return

    joined = ", ".join(config_keys)
    raise AuthRejected(
        f"Unauthorized: worker access denied by project policy "
        f"(all of: {joined} are off). Ask admin to enable "
        "one in dashboard Settings."
    )


def requires_policy(
    *config_keys: str,
    default: Optional[bool] = None,
) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point with the worker-toggle pattern.

    Operator-tier callers (cookie / forwarding-header / sysadmin)
    always pass. Worker tokens pass iff *at least one* of the listed
    ``config_keys`` resolves truthy in ``project_settings``; the
    per-key default (used when the row is absent) is resolved from the
    single-source schema registry (ADR-0018) unless ``default=`` is
    passed explicitly. ``_get_config_bool`` performs the registry
    fallback, so the per-key default can never drift from
    :data:`agent_mcp.tools.access._TOGGLE_DEFAULTS` (both derive from
    ``core/settings_schema``).

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
            # synthesizing one from the ``request_auth_token``
            # ContextVar (token-retirement PR 2).
            if principal is None:
                principal = _synthesize_principal_from_arguments(arguments)
            check_policy_gate(principal, config_keys, default)
            return await _call(arguments, principal, kwargs)

        # PR-W1c (2026-06-05): expose the toggle keys + default on the
        # wrapper so the derived TOOL_ACCESS map can rebuild the
        # `worker-if-toggled:<keys>` access level string without
        # re-parsing the source. The level string keeps any-of
        # semantics (matches `is_visible_to_role`).
        wrapper._required_policy_keys = tuple(config_keys)  # type: ignore[attr-defined]
        wrapper._required_policy_default = default  # type: ignore[attr-defined]
        return wrapper

    return decorator


PredicateFn = Callable[[Optional[Principal]], bool]


def agent_bearer_with_capability(cap: str) -> PredicateFn:
    """Predicate: an ``agent_bearer`` Principal that carries ``cap``.

    The recurring "agent-only AND capability-gated" shape (four tools
    at time of writing: ``ask_project_rag`` on ``rag.query``,
    ``check_file_status`` / ``update_file_status`` /
    ``view_file_metadata`` on ``files.use``). Both halves are load-
    bearing and neither can be dropped:

    * the ``kind`` half keeps operator-session callers out — those
      tools key on ``agent_id``, which an operator doesn't carry, and
      operators DO hold the caps in their project bundle, so a bare
      capability check would widen them;
    * the capability half keeps an ``agent_role``-less bearer (empty
      capability bundle) out — the empty-bearer class closed in SEC
      Wave-B / SEC round 2.

    Defined once here rather than per-module so the four copies cannot
    drift; pass it to :func:`requires_predicate` with a tool-specific
    reason.
    """

    def _predicate(principal: Optional[Principal]) -> bool:
        return (
            principal is not None
            and principal.kind == "agent_bearer"
            and principal.has_capability(cap)
        )

    return _predicate


def check_predicate_gate(
    principal: Optional[Principal],
    predicate: PredicateFn,
    reason: str,
) -> None:
    """Raise :class:`AuthRejected` iff ``predicate(principal)`` is falsy.

    R21-F1: generalizes :func:`check_capability_gate` /
    :func:`check_policy_gate` for tools whose authorization isn't a
    single capability string or toggle set but an arbitrary boolean
    check over the Principal — e.g. ``is_operator_tier`` (capability
    OR the legacy ``agent_id == "admin"`` label the test harness
    seeds). Shared by the :func:`requires_predicate` wrapper AND
    ``dispatch_tool_call``'s pre-schema-validation gate, so the two
    evaluations can never diverge (same rationale as the capability /
    policy gates).
    """
    if not predicate(principal):
        raise AuthRejected(reason)


def requires_predicate(
    predicate: PredicateFn,
    reason: str,
) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against an arbitrary Principal predicate.

    R21-F1: for tools whose authorization was an in-body call to a
    shared boolean helper that is MORE than a single capability check
    (e.g. ``_is_operator_tier`` — a capability OR the legacy
    ``agent_id == "admin"`` label) rather than
    ``@requires_capability`` / ``@requires_policy``. A helper that
    reduces to a single ``principal.has_capability(cap)`` check should
    use ``@requires_capability(cap)`` directly instead — this decorator
    is for the cases that genuinely need an arbitrary predicate. Those
    helpers set no ``_required_capability`` / ``_required_policy_keys``
    attribute, so ``dispatch_tool_call``'s R20-F4 pre-schema gate
    couldn't see them — a malformed call from an unauthorized caller
    reached ``jsonschema.validate`` first, leaking the tool's exact
    schema shape. Wrapping the SAME predicate in this decorator stamps
    ``_required_predicate`` (mirroring ``_required_capability``) so the
    dispatcher's pre-schema gate covers it too, with no change to the
    predicate itself — the authorization DECISION is identical, only
    WHEN it runs moves earlier.

    Pass ``reason`` as the exact ``AuthRejected`` message; keep it
    short (it reaches agent transcripts / REST error bodies verbatim).
    """

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
            check_predicate_gate(principal, predicate, reason)
            if forward_principal:
                return await func(arguments, principal=principal, **kwargs)
            return await func(arguments, **kwargs)

        # Exposed for `dispatch_tool_call`'s pre-schema gate (registry.py).
        # Deliberately NOT consulted by `tools.access._derive_access_level`
        # — an arbitrary predicate can't be mapped to a worker/manager/
        # operator visibility tier the way a capability or toggle set can,
        # so predicate-gated tools keep using the `visibility=` kwarg as
        # their sole `tools/list` signal (same as an in-body cap check).
        wrapper._required_predicate = predicate  # type: ignore[attr-defined]
        wrapper._required_predicate_reason = reason  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ── Registration-time requirement declarations (Phase 2, Finding A) ──
#
# The decorators above are where enforcement LIVES: they wrap the impl,
# so the gate travels with the function object and fires for the
# in-process callers that invoke a tool impl directly
# (``app/routers/agents.py``, ``app/routers/schedules.py``,
# ``task_tools.request_assistance``, ``broadcast_admin_message``'s
# fan-out, ``task_placement.validator``). Moving enforcement to
# registration would silently un-gate every one of those call sites, so
# it stays on the decorator.
#
# What was missing is a DECLARATION at the catalogue: nothing forced a
# tool author to state an authorization story at all, and
# ``register_tool`` happily accepted an impl with no gate whatsoever.
# That is the "opt-in-and-forget" shape (OBS-R11-1) this finding closes.
# The types below are that declaration. ``register_tool`` requires one
# and VERIFIES it against the decorator's stamp at import time, so:
#
#   * registering a tool without stating its authorization is impossible
#     (``requires=`` has no default);
#   * claiming a requirement the impl doesn't actually enforce is an
#     ImportError, not a silent lie (the class of drift ``access.py``'s
#     module docstring warns about);
#   * ``PUBLIC`` is the only way to register an ungated tool, and it is
#     greppable, reviewable, and pinned by
#     ``tests/test_arch_enforced_tool_capability_registration.py``'s
#     allowlist.


class ToolRequirement:
    """Base class for the ``register_tool(requires=...)`` vocabulary."""

    __slots__ = ()

    def verify(self, impl: Callable) -> Optional[str]:
        """Return an error string if ``impl``'s stamp contradicts this
        declaration, else None."""
        raise NotImplementedError  # pragma: no cover - abstract


def _stamp_of(impl: Callable) -> str:
    """Human-readable description of what ``impl`` actually enforces."""
    cap = getattr(impl, "_required_capability", None)
    if cap is not None:
        return f"@requires_capability({cap!r})"
    policy_keys = getattr(impl, "_required_policy_keys", None)
    if policy_keys:
        return f"@requires_policy{tuple(policy_keys)!r}"
    if getattr(impl, "_required_predicate", None) is not None:
        reason = getattr(impl, "_required_predicate_reason", None)
        return f"@requires_predicate(reason={reason!r})"
    return "no @requires_* decorator"


class Cap(ToolRequirement):
    """The tool is gated on exactly one capability.

    Must match a ``@requires_capability(cap)`` on the implementation.
    """

    __slots__ = ("cap",)

    def __init__(self, cap: str) -> None:
        self.cap = cap

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Cap({self.cap!r})"

    def verify(self, impl: Callable) -> Optional[str]:
        actual = getattr(impl, "_required_capability", None)
        if actual == self.cap:
            return None
        return (
            f"declares requires=Cap({self.cap!r}) but the implementation "
            f"carries {_stamp_of(impl)}"
        )


class Policy(ToolRequirement):
    """The tool is gated on the worker-toggle policy over ``keys``.

    Must match a ``@requires_policy(*keys, default=...)`` on the
    implementation.
    """

    __slots__ = ("keys", "default")

    def __init__(self, *keys: str, default: Optional[bool] = None) -> None:
        self.keys = tuple(keys)
        self.default = default

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Policy{self.keys!r}"

    def verify(self, impl: Callable) -> Optional[str]:
        actual = getattr(impl, "_required_policy_keys", None)
        actual_default = getattr(impl, "_required_policy_default", None)
        if tuple(actual or ()) == self.keys and actual_default == self.default:
            return None
        return (
            f"declares requires=Policy{self.keys!r} (default={self.default!r}) "
            f"but the implementation carries {_stamp_of(impl)}"
        )


class Predicate(ToolRequirement):
    """The tool is gated on an arbitrary Principal predicate.

    Identified by the denial ``reason`` rather than by the function
    object, so the registration site doesn't have to import (and
    re-state) the predicate itself. ``reason`` is the user-visible
    denial text, which is the part worth pinning at the catalogue.

    Predicate-gated tools cannot derive a ``tools/list`` tier
    (``requires_predicate``'s docstring explains why), so these are
    exactly the tools that must keep an explicit ``visibility=`` kwarg
    when they are not ``"any"``.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Predicate({self.reason!r})"

    def verify(self, impl: Callable) -> Optional[str]:
        if getattr(impl, "_required_predicate", None) is None:
            return (
                "declares requires=Predicate(...) but the implementation "
                f"carries {_stamp_of(impl)}"
            )
        actual_reason = getattr(impl, "_required_predicate_reason", None)
        if actual_reason != self.reason:
            return (
                "declares requires=Predicate(reason=...) whose text differs "
                f"from the implementation's: declared {self.reason!r}, "
                f"actual {actual_reason!r}"
            )
        return None


class _Public(ToolRequirement):
    """The tool requires NO authorization. See :data:`PUBLIC`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "PUBLIC"

    def verify(self, impl: Callable) -> Optional[str]:
        stamp = _stamp_of(impl)
        if stamp == "no @requires_* decorator":
            return None
        return (
            f"declares requires=PUBLIC but the implementation carries {stamp} "
            "— an enforced gate must be declared, not hidden behind PUBLIC"
        )


#: Explicit "this tool is callable by anyone, including unauthenticated
#: callers". The ONLY way to register a tool with no authorization, and
#: deliberately a named constant so it greps. Adding one is a security
#: decision: justify it at the call site and in the allowlist in
#: ``tests/test_arch_enforced_tool_capability_registration.py``.
PUBLIC = _Public()
