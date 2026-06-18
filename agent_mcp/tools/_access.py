"""Per-tool access decorator with introspectable role attribute.

Sibling of :mod:`agent_mcp.tools.access` (the visibility table). This
module hosts the *enforcement* decorator that the dispatcher's auth
seam relies on at call time.

Architecture review PR-W1c (2026-06-05) replaced the single
hand-maintained ``agent_mcp/tools/access.py::TOOL_ACCESS`` dict with a
*double source of truth* for per-tool access policy:

1. **``@requires_role(role)``** decorator on the impl — this module.
   Enforces the auth check at the call site (most secure surface).
   Exposes the role via ``fn._required_role`` for introspection so the
   derived visibility map can find it.

2. **``visibility=`` kwarg** on ``register_tool()`` — see
   :mod:`agent_mcp.tools.registry`. Makes the same fact visible to the
   registry / ``tools/list`` filter / UI / policy code at registration
   time. Pure metadata; the decorator is the actual gate.

``agent_mcp/tools/access.py::TOOL_ACCESS`` becomes a *derived*
callable that introspects every entry's ``_required_role`` (from this
decorator) and the registered ``visibility`` (from the kwarg).
Decorator wins when they disagree (the call-site enforcement is the
real authority; the kwarg merely surfaces it).

Why a new name?
---------------

The existing :func:`agent_mcp.core.authorize.requires` decorator (from
the 2026-06-01 architecture review, candidate A) already wraps tool
entry points with ``verify_token(...)``. ``requires_role`` is an
alias that adds the ``_required_role`` attribute on the wrapper so
the derived access map can introspect it. Code that uses the existing
``@requires("admin")`` keeps working unchanged — this module
re-exports the underlying behavior via :func:`requires_role` and the
registry's introspection treats both as equivalent.

The name also lives under ``agent_mcp/tools/_access.py`` (note the
leading underscore on the module to mark it as the tools-package
private detail) per the PR-W1c spec.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

import mcp.types as mcp_types

from ..core.authorize import requires_role as _core_requires_role


ToolImpl = Callable[[Dict[str, Any]], Awaitable[List[mcp_types.TextContent]]]


def requires_role(role: str) -> Callable[[ToolImpl], ToolImpl]:
    """Authorise a tool entry point against a static role *and* expose
    the role for introspection.

    Thin re-export of :func:`agent_mcp.core.authorize.requires_role` so
    tool modules can ``from .tools._access import requires_role``
    without crossing the core boundary. The core decorator already
    sets ``_required_role`` on the wrapper, so the derived
    :data:`agent_mcp.tools.access.TOOL_ACCESS` map can discover the
    policy without re-parsing the source.

    ``role`` is one of ``"operator"``, ``"manager"``, ``"any"``, or
    the legacy alias ``"admin"`` (== ``"operator"``).
    """
    return _core_requires_role(role)


__all__ = ["requires_role"]
