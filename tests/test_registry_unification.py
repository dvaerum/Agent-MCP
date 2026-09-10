"""Unification invariants for the new `agent_mcp.core.registry.Registry[T]`.

Candidate B from the 2026-06-02 architecture review. Three subsystems —
tools, resources, prompts — used to each invent their own
register/list/dispatch shape. This module pins the contract of the
shared `Registry[T]` abstraction they now share, plus the tool-registry
adaptor and visibility filtering.

These tests are intentionally close to the API surface — they're the
spec, not a behavior-by-behavior dump of every adaptor's existing
test file (those continue to live alongside the subsystem they
cover). Failing here means the unification contract regressed.

The resources/prompts adaptor sections (Candidate G's prompt-visibility
unification, and the resource-registry/MCP-wire tests) were pinned here
too until Phase F's `agent_mcp/prompts/`/`agent_mcp/resources/`
deletion retired them.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. Generic Registry[T] — register / list_visible / get on arbitrary T.
# ---------------------------------------------------------------------------


def test_generic_registry_register_and_get_roundtrip() -> None:
    """A fresh Registry holds whatever payload type T we give it; get
    by name returns the same RegistryEntry that was registered."""
    from agent_mcp.core.registry import Registry, RegistryEntry

    reg: Registry[str] = Registry()
    entry = RegistryEntry(name="hello", visibility="any", meta="world")
    reg.register(entry)

    fetched = reg.get("hello")
    assert fetched is not None
    assert fetched.name == "hello"
    assert fetched.meta == "world"
    assert reg.get("missing") is None


def test_generic_registry_list_visible_admin_sees_all() -> None:
    """Admin role bypasses every visibility filter — admin sees the
    full catalogue."""
    from agent_mcp.core.registry import Registry, RegistryEntry

    reg: Registry[int] = Registry()
    reg.register(RegistryEntry(name="a", visibility="any", meta=1))
    reg.register(RegistryEntry(name="b", visibility="admin", meta=2))
    reg.register(
        RegistryEntry(name="c", visibility=lambda role: role == "worker", meta=3)
    )

    names = {e.name for e in reg.list_visible("admin")}
    assert names == {"a", "b", "c"}


def test_generic_registry_list_visible_worker_filters_admin_only() -> None:
    """Worker role sees `any`-visibility + any policy-callable that
    returns True for "worker", but NOT bare admin-visibility entries."""
    from agent_mcp.core.registry import Registry, RegistryEntry

    reg: Registry[int] = Registry()
    reg.register(RegistryEntry(name="public", visibility="any", meta=1))
    reg.register(RegistryEntry(name="admin-only", visibility="admin", meta=2))
    reg.register(
        RegistryEntry(
            name="worker-policy",
            visibility=lambda role: role == "worker",
            meta=3,
        )
    )

    names = {e.name for e in reg.list_visible("worker")}
    assert names == {"public", "worker-policy"}, (
        f"worker should see 'any' + policy-true entries; got {names}"
    )


def test_generic_registry_list_visible_anonymous_only_any() -> None:
    """Anonymous role sees only "any" — both "admin" and arbitrary
    policy callables that don't whitelist anonymous are hidden."""
    from agent_mcp.core.registry import Registry, RegistryEntry

    reg: Registry[int] = Registry()
    reg.register(RegistryEntry(name="public", visibility="any", meta=1))
    reg.register(RegistryEntry(name="admin-only", visibility="admin", meta=2))
    reg.register(
        RegistryEntry(
            name="worker-policy",
            visibility=lambda role: role == "worker",
            meta=3,
        )
    )

    names = {e.name for e in reg.list_visible("anonymous")}
    assert names == {"public"}


def test_generic_registry_duplicate_register_overwrites_with_warning(
    caplog,
) -> None:
    """Re-registering the same name overwrites (matches the existing
    `tools.registry.register_tool` behavior) and logs a warning."""
    from agent_mcp.core.registry import Registry, RegistryEntry

    reg: Registry[str] = Registry()
    reg.register(RegistryEntry(name="x", visibility="any", meta="first"))
    reg.register(RegistryEntry(name="x", visibility="any", meta="second"))

    assert reg.get("x").meta == "second"


# ---------------------------------------------------------------------------
# 2. ToolRegistry — backwards-compatible adaptor over the shared core.
# ---------------------------------------------------------------------------


def test_tool_registry_reflects_legacy_register_tool() -> None:
    """`register_tool(...)` from `agent_mcp.tools.registry` continues
    to populate the shared registry. Every tool that lives in
    `tool_schemas` after import is also present in the shared
    `tool_registry`'s entries."""
    import agent_mcp.tools  # noqa: F401 — triggers registration
    from agent_mcp.tools.registry import tool_registry, tool_schemas

    registered = {e["name"] for e in tool_schemas}
    shared = {e.name for e in tool_registry.list_visible("admin")}
    missing = registered - shared
    assert not missing, (
        f"tools registered via register_tool() not in shared "
        f"tool_registry: {missing}"
    )


def test_tool_registry_visibility_matches_access_table() -> None:
    """Every tool's RegistryEntry visibility filter agrees with
    `is_visible_to_role` for the worker role (the role with the most
    discriminating filter)."""
    import agent_mcp.tools  # noqa: F401
    from agent_mcp.tools.access import is_visible_to_role
    from agent_mcp.tools.registry import tool_registry, tool_schemas

    for entry in tool_schemas:
        name = entry["name"]
        re = tool_registry.get(name)
        assert re is not None, f"missing shared entry for tool {name}"
        # Both paths must agree; compute via the entry's visibility
        # resolver (string sentinel or callable) and compare.
        from agent_mcp.core.registry import resolve_visibility

        for role in ("admin", "worker", "anonymous"):
            via_entry = resolve_visibility(re.visibility, role)
            via_access = is_visible_to_role(name, role)
            assert via_entry == via_access, (
                f"visibility disagrees for tool={name!r} role={role!r}: "
                f"entry={via_entry} access={via_access}"
            )


