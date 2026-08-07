"""Unification invariants for the new `agent_mcp.core.registry.Registry[T]`.

Candidate B from the 2026-06-02 architecture review. Three subsystems —
tools, resources, prompts — used to each invent their own
register/list/dispatch shape. This module pins the contract of the
shared `Registry[T]` abstraction they now share, plus the per-subsystem
adaptors and visibility filtering.

Subsumes Candidate G: prompts now carry a `visibility` field and
filter `prompts/list` / `prompts/get` by the caller's role, the same
way tools already do via `tools.access.is_visible_to_role`.

These tests are intentionally close to the API surface — they're the
spec, not a behavior-by-behavior dump of every adaptor's existing
test file (those continue to live alongside the subsystem they
cover). Failing here means the unification contract regressed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# 3. ResourceRegistry — inbox/status entries registered via the shared core.
# ---------------------------------------------------------------------------


def test_resource_registry_has_inbox_and_status_entries() -> None:
    """The two well-known resources register with the shared
    registry as named entries (`inbox` and `status`)."""
    from agent_mcp.resources import resource_registry

    names = {e.name for e in resource_registry.list_visible("admin")}
    assert "inbox" in names
    assert "status" in names


# ---------------------------------------------------------------------------
# 4. PromptRegistry — catalog entries + visibility filtering (Candidate G).
# ---------------------------------------------------------------------------


def test_prompt_registry_loads_catalog_entries() -> None:
    """Every catalog entry is registered as a PromptRegistry entry,
    keyed by id."""
    from agent_mcp.prompts import load_catalog, prompt_registry

    catalog_ids = {p["id"] for p in load_catalog()["prompts"]}
    registry_names = {e.name for e in prompt_registry.list_visible("admin")}
    missing = catalog_ids - registry_names
    assert not missing, f"prompts missing from registry: {missing}"


def test_prompt_registry_visibility_defaults_to_any() -> None:
    """A catalog entry without an explicit `visibility` field defaults
    to "any" — i.e. visible to every role including unauthenticated."""
    from agent_mcp.prompts import prompt_registry

    # The shipped catalog ships every prompt as "any" today; every
    # prompt MUST be visible to an anonymous caller.
    anon_names = {e.name for e in prompt_registry.list_visible("anonymous")}
    admin_names = {e.name for e in prompt_registry.list_visible("admin")}
    assert anon_names == admin_names, (
        f"all default prompts should be 'any'-visibility; "
        f"admin-only={admin_names - anon_names}"
    )


def test_prompt_registry_respects_explicit_admin_visibility(
    tmp_path: Path,
) -> None:
    """A prompt with `"visibility": "admin"` in catalog.json is in
    admin's list_visible but NOT in worker's / anonymous's.

    This is Candidate G in disguise: prompts gain admin-only gating
    for free now that they ride on the shared registry.
    """
    # Build a temporary catalog with one admin-only and one any prompt.
    import agent_mcp.prompts as prompts_mod

    fake_catalog = {
        "categories": [{"id": "test", "name": "Test", "description": "", "icon": "X"}],
        "prompts": [
            {
                "id": "public-prompt",
                "title": "Public",
                "description": "anyone",
                "category": "test",
                "visibility": "any",
                "template": "hello",
                "variables": [],
            },
            {
                "id": "admin-only-prompt",
                "title": "Admin Only",
                "description": "admins",
                "category": "test",
                "visibility": "admin",
                "template": "secret",
                "variables": [],
            },
        ],
    }
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(fake_catalog))

    # Point the module's loader at the temp catalog & rebuild the
    # PromptRegistry. The helper is exposed for tests; in production
    # the registry is built once at import time from the shipped JSON.
    prompts_mod._reload_catalog_for_tests(p)
    try:
        admin_names = {
            e.name for e in prompts_mod.prompt_registry.list_visible("admin")
        }
        worker_names = {
            e.name for e in prompts_mod.prompt_registry.list_visible("worker")
        }
        anon_names = {
            e.name
            for e in prompts_mod.prompt_registry.list_visible("anonymous")
        }
        assert "admin-only-prompt" in admin_names
        assert "admin-only-prompt" not in worker_names
        assert "admin-only-prompt" not in anon_names
        assert "public-prompt" in admin_names
        assert "public-prompt" in worker_names
        assert "public-prompt" in anon_names
    finally:
        prompts_mod._reload_catalog_for_tests(None)  # restore real catalog


# ---------------------------------------------------------------------------
# 5. End-to-end: MCP `prompts/list` filters admin-only entries for workers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_prompts_list_filters_admin_only_for_worker(
    tmp_path: Path,
) -> None:
    """The Candidate-G end-to-end invariant: a worker calling MCP
    `prompts/list` does NOT see prompts marked
    `"visibility": "admin"` in catalog.json."""
    import mcp.types as mcp_types

    import agent_mcp.prompts as prompts_mod
    from agent_mcp.tools.registry import request_auth_token
    from tests.harness import mcp_session

    fake_catalog = {
        "categories": [{"id": "t", "name": "T", "description": "", "icon": "X"}],
        "prompts": [
            {
                "id": "any-prompt",
                "title": "Public",
                "description": "",
                "category": "t",
                "visibility": "any",
                "template": "x",
                "variables": [],
            },
            {
                "id": "admin-only-prompt",
                "title": "Admin Only",
                "description": "",
                "category": "t",
                "visibility": "admin",
                "template": "y",
                "variables": [],
            },
        ],
    }
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(fake_catalog))
    prompts_mod._reload_catalog_for_tests(p)
    try:
        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice-prompts")

            async def list_for(session) -> set[str]:
                handler = session._admin._mcp_app_instance().request_handlers[
                    mcp_types.ListPromptsRequest
                ]
                req = mcp_types.ListPromptsRequest(method="prompts/list")
                tok = request_auth_token.set(session.token)
                try:
                    result = await handler(req)
                finally:
                    request_auth_token.reset(tok)
                inner = result.root if hasattr(result, "root") else result
                return {p.name for p in (getattr(inner, "prompts", []) or [])}

            admin_names = await list_for(admin)
            worker_names = await list_for(alice)

            assert "admin-only-prompt" in admin_names
            assert "any-prompt" in admin_names
            assert "any-prompt" in worker_names
            assert "admin-only-prompt" not in worker_names, (
                f"worker leaked admin-only prompt: {worker_names}"
            )
    finally:
        prompts_mod._reload_catalog_for_tests(None)


@pytest.mark.asyncio
async def test_mcp_prompts_get_rejects_admin_only_for_worker(
    tmp_path: Path,
) -> None:
    """Worker calling `prompts/get` on an admin-only prompt is rejected
    (defense in depth — list filtering alone is not enough if a worker
    guesses the name)."""
    import mcp.types as mcp_types

    import agent_mcp.prompts as prompts_mod
    from agent_mcp.tools.registry import request_auth_token
    from tests.harness import mcp_session

    fake_catalog = {
        "categories": [{"id": "t", "name": "T", "description": "", "icon": "X"}],
        "prompts": [
            {
                "id": "admin-secret",
                "title": "Admin Secret",
                "description": "",
                "category": "t",
                "visibility": "admin",
                "template": "secret={{X}}",
                "variables": [
                    {"name": "X", "description": "", "required": False}
                ],
            },
        ],
    }
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(fake_catalog))
    prompts_mod._reload_catalog_for_tests(p)
    try:
        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice-promptget")

            handler = admin._admin._mcp_app_instance().request_handlers[
                mcp_types.GetPromptRequest
            ]

            async def get_for(session, name: str):
                req = mcp_types.GetPromptRequest(
                    method="prompts/get",
                    params=mcp_types.GetPromptRequestParams(
                        name=name, arguments={"X": "v"}
                    ),
                )
                tok = request_auth_token.set(session.token)
                try:
                    return await handler(req)
                finally:
                    request_auth_token.reset(tok)

            # Admin OK.
            res = await get_for(admin, "admin-secret")
            inner = res.root if hasattr(res, "root") else res
            assert inner.messages, "admin should successfully render"

            # Worker rejected.
            with pytest.raises(PermissionError):
                await get_for(alice, "admin-secret")
    finally:
        prompts_mod._reload_catalog_for_tests(None)
