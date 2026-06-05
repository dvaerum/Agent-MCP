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


def requires(role: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a static role.

    ``role`` is one of:

    * ``"admin"`` — only the admin token is accepted.
    * ``"any"`` — any *currently active* agent token is accepted
      (admin token also counts; it can act as an agent per
      :func:`agent_mcp.core.auth.verify_token`'s rule).

    Raises :class:`AuthRejected` on miss. The wording matches what the
    old per-tool ``if not verify_token(...): return TextContent(
    "Unauthorized: ...")`` blocks produced so existing clients that
    string-match on the message keep working.
    """
    if role not in ("admin", "any"):
        raise ValueError(
            f"@requires(role={role!r}) — role must be 'admin' or 'any'"
        )

    def decorator(func: ToolImpl) -> ToolImpl:
        @functools.wraps(func)
        async def wrapper(
            arguments: Dict[str, Any],
        ) -> List[mcp_types.TextContent]:
            token = _extract_token(arguments)
            if role == "admin":
                if not verify_token(token, "admin"):
                    raise AuthRejected("Unauthorized: Admin token required")
            else:  # role == "any"
                if not get_agent_id(token):
                    raise AuthRejected("Unauthorized: Valid token required")
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
            if verify_token(token, "admin"):
                return await func(arguments)

            # Worker path: must resolve to an active agent first.
            if not get_agent_id(token):
                raise AuthRejected("Unauthorized: Valid token required")

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
