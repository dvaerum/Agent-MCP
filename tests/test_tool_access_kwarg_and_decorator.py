"""PR-W1c — `visibility=` kwarg + `@requires_role` decorator (double SoT).

Before this PR the tool-access policy lived in exactly one place:
`agent_mcp/tools/access.py::TOOL_ACCESS`, a hand-maintained dict that
each `tools/list` call consulted. The fact "this tool is admin-only"
was *not* visible at the tool's registration site nor at the impl —
the call-time auth check was a separately-implemented
`@requires("admin")` decorator (or, pre-PR-#15, an inline
`verify_token(...)` call) whose presence had to manually stay in sync
with the access table.

This PR introduces a double source of truth:

1. **`@requires_role(role)`** decorator on each impl — enforces the
   auth check at the call site (most secure) and exposes the role via
   the `_required_role` attribute for introspection.

2. **`visibility=`** kwarg on `register_tool()` — makes the same fact
   visible to the registry / tools/list / UI / policy code at
   registration time.

`access.py::TOOL_ACCESS` becomes *derived* from the registry: a
function that introspects every entry's recorded `visibility` and the
impl's `_required_role` attribute. The existing invariant test
(`test_every_registered_tool_has_access_classification`) keeps
passing because the derived dict still answers "is this tool
classified?" for every registered name.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

import mcp.types as mcp_types
import pytest


# --- Test A: both decorator + kwarg, decorator enforces ---


@pytest.mark.asyncio
async def test_decorator_and_kwarg_admin_only_blocks_worker() -> None:
    """Register a fake tool with `visibility="admin"` kwarg AND
    `@requires_role("admin")` decorator. A worker bearer must be
    rejected (decorator raises AuthRejected); an admin bearer must
    succeed.
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools._access import requires_role
    from agent_mcp.tools.registry import register_tool, tool_registry

    @requires_role("admin")
    async def _fake_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="ok")]

    register_tool(
        name="_test_admin_dec_and_kwarg",
        description="test",
        input_schema={"type": "object", "properties": {"token": {"type": "string"}}},
        implementation=_fake_impl,
        visibility="admin",
    )

    try:
        # Worker bearer → AuthRejected.
        with pytest.raises(AuthRejected):
            await _fake_impl({"token": "definitely-not-admin"})

        # Admin bearer → succeeds. Use the live admin token from auth.
        from agent_mcp.core import auth as _auth
        admin_token = _auth.admin_token
        if not admin_token:
            # Auth module hasn't been bootstrapped (no project loaded).
            # Set one for this test.
            _auth.admin_token = "test-admin-token-for-w1c"
            admin_token = _auth.admin_token
        try:
            result = await _fake_impl({"token": admin_token})
            assert result[0].text == "ok"
        finally:
            _auth.admin_token = ""
    finally:
        # Clean up registry state to avoid polluting other tests.
        if "_test_admin_dec_and_kwarg" in tool_registry.names():
            tool_registry._entries.pop("_test_admin_dec_and_kwarg", None)


# --- Test B: decorator-only (no kwarg) — decorator still enforces ---


@pytest.mark.asyncio
async def test_decorator_only_still_enforces_at_call_site() -> None:
    """Register a fake tool with `@requires_role("admin")` decorator
    but NO `visibility=` kwarg (defaults to "any"). The decorator must
    still reject a worker bearer at call time — the kwarg is metadata
    for tools/list filtering, the decorator is the enforcement seam.
    """
    from agent_mcp.core.authorize import AuthRejected
    from agent_mcp.tools._access import requires_role
    from agent_mcp.tools.registry import register_tool, tool_registry

    @requires_role("admin")
    async def _fake_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="ok")]

    register_tool(
        name="_test_admin_dec_only",
        description="test",
        input_schema={"type": "object", "properties": {"token": {"type": "string"}}},
        implementation=_fake_impl,
        # NO visibility= kwarg — uses default.
    )

    try:
        # Worker bearer → AuthRejected from decorator.
        with pytest.raises(AuthRejected):
            await _fake_impl({"token": "not-admin"})

        # The decorator exposes the role via _required_role for
        # introspection (used by the derived TOOL_ACCESS).
        assert getattr(_fake_impl, "_required_role", None) == "admin"
    finally:
        tool_registry._entries.pop("_test_admin_dec_only", None)


# --- Test C: kwarg-only (no decorator) — kwarg surfaces in access map ---


def test_kwarg_only_reports_admin_in_derived_access_map() -> None:
    """Register a fake tool with `visibility="admin"` kwarg but NO
    decorator. The derived `TOOL_ACCESS` map must report it as
    "admin" (the kwarg is the metadata source). Call-site
    enforcement would slip through here — that's the point of the
    double SoT: the kwarg surfaces the policy for tools/list +
    UI even when the decorator was forgotten.
    """
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import register_tool, tool_registry

    async def _fake_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        # No decorator. (In practice this would be a bug — but the
        # kwarg should still surface the policy.)
        return [mcp_types.TextContent(type="text", text="ok")]

    register_tool(
        name="_test_admin_kwarg_only",
        description="test",
        input_schema={"type": "object", "properties": {}},
        implementation=_fake_impl,
        visibility="admin",
    )

    try:
        # TOOL_ACCESS is derived from the registry; re-evaluating it
        # must reflect the just-registered tool.
        if callable(TOOL_ACCESS):
            access = TOOL_ACCESS()
        else:
            access = TOOL_ACCESS
        assert access.get("_test_admin_kwarg_only") == "admin"
    finally:
        tool_registry._entries.pop("_test_admin_kwarg_only", None)


# --- Test D: derived TOOL_ACCESS preserves invariant ---


def test_derived_tool_access_classifies_every_registered_tool() -> None:
    """The existing invariant
    (`test_every_registered_tool_has_access_classification`) must
    continue to hold after the access.py refactor: every registered
    tool name appears in `TOOL_ACCESS`.

    This pins the *derivation* itself, not just the table — proves
    the introspection path covers every real tool.
    """
    import agent_mcp.tools  # noqa: F401 — triggers register_*_tools()
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import tool_schemas

    registered = {e["name"] for e in tool_schemas}
    if callable(TOOL_ACCESS):
        classified = set(TOOL_ACCESS().keys())
    else:
        classified = set(TOOL_ACCESS.keys())
    missing = sorted(registered - classified)
    assert not missing, (
        f"derived TOOL_ACCESS missed these registered tools: {missing}"
    )


# --- Test E: derivation prefers decorator when both present and disagree ---


def test_derivation_admin_when_decorator_says_admin_kwarg_says_any() -> None:
    """If the decorator says admin but the kwarg says "any" (or
    vice-versa), the derived map should report admin — the
    enforcement-side wins. This prevents accidental hole punching:
    if someone marks a tool `visibility="any"` but its impl is
    decorated `@requires_role("admin")`, the tool is admin-only at
    call time, and the visibility map should reflect that so
    tools/list doesn't advertise it to workers.
    """
    from agent_mcp.tools._access import requires_role
    from agent_mcp.tools.access import TOOL_ACCESS
    from agent_mcp.tools.registry import register_tool, tool_registry

    @requires_role("admin")
    async def _fake_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
        return [mcp_types.TextContent(type="text", text="ok")]

    register_tool(
        name="_test_disagree_dec_admin_kwarg_any",
        description="test",
        input_schema={"type": "object", "properties": {}},
        implementation=_fake_impl,
        visibility="any",
    )

    try:
        access = TOOL_ACCESS() if callable(TOOL_ACCESS) else TOOL_ACCESS
        assert access.get("_test_disagree_dec_admin_kwarg_any") == "admin", (
            "decorator's role must win over kwarg in derivation; "
            "tools/list must not advertise an admin-decorated tool to workers"
        )
    finally:
        tool_registry._entries.pop("_test_disagree_dec_admin_kwarg_any", None)


# --- Test F: requires_role is importable from _access ---


def test_requires_role_decorator_exists_with_expected_signature() -> None:
    """The PR spec mandates `agent_mcp/tools/_access.py::requires_role`
    as the introspectable decorator surface. Test it imports and has
    the expected shape (one positional `role` argument).
    """
    from agent_mcp.tools._access import requires_role

    sig = inspect.signature(requires_role)
    params = list(sig.parameters)
    assert params == ["role"], (
        f"requires_role(role) signature drifted: got {params}"
    )
