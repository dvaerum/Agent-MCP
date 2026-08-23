"""Phase 4 / Finding E: the shared ``decide()`` authorization seam.

``agent_mcp/core/registry.py``'s ``RegistryEntry.visibility`` is
authoritative for ``resources/list`` (``Registry.list_visible``) on all
three MCP catalogs, but before this change only Prompts re-checked it at
verb time (``PromptRegistry.render`` re-runs ``resolve_visibility`` —
"a worker could otherwise guess the id"). The Resources read path ran a
DIFFERENT, parallel gate (``resolve_agent_id_for_uri``'s
``catalog_role(principal) == "admin"`` vs. own-agent_id scoping) that
never consulted ``entry.visibility`` at all — benign only because both
shipped resources are ``visibility="any"``.

This module pins the seam that closes that:

* the R21-F4 property (an operator-tier admin, ``agent_id=None``, reads
  another agent's resource) now expressed through ``decide()``;
* the cross-agent IDOR regression guard (a non-admin bearer reading
  another agent's resource is still denied), through ``decide()``;
* the gap itself — a ``visibility="admin"`` resource is denied to a
  non-admin caller on the READ path, even when the URI addresses that
  caller's OWN agent_id. The old ``resolve_agent_id_for_uri``-only
  mechanism admitted that read unconditionally.

The role used for the visibility gate is
``core.principal_builder.catalog_role`` — the same function the old
admin branch used and the same one ``resources/list`` filters on — so
list-visibility and read-visibility cannot drift apart per caller shape.
"""

from __future__ import annotations

import pytest

_SECRET_PREFIX = "agent-mcp://secret/"


# ── Principal fixtures: every shape reachable in production ──────────


def _sysadmin_operator():
    """Cookie-session sysadmin. ``agent_id`` is None by construction."""
    from agent_mcp.core.principal_builder import build_operator_principal

    return build_operator_principal(
        user_id="sysadmin-user",
        kind="operator_session",
        project_role=None,
        sysadmin=True,
    )


def _forwarding_operator():
    """Router's signed forwarding-header proxy path, operator role."""
    from agent_mcp.core.principal_builder import build_operator_principal

    return build_operator_principal(
        user_id="router-forwarded-admin",
        kind="forwarding_header",
        project_role="operator",
        sysadmin=False,
    )


def _forwarding_viewer():
    """Viewer-tier forwarding-header caller — authenticated, NOT admin."""
    from agent_mcp.core.principal_builder import build_operator_principal

    return build_operator_principal(
        user_id="viewer-user",
        kind="forwarding_header",
        project_role="viewer",
        sysadmin=False,
    )


def _agent_bearer(agent_id: str = "alice", agent_role: str = "worker"):
    from agent_mcp.core.principal import Principal

    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=agent_role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token=f"{agent_id}-token",
        capabilities=frozenset(),
    )


def _legacy_admin_bearer():
    """The harness's legacy ``agent_id == "admin"`` pseudo-agent — an
    agent-bearer that ``is_operator_tier`` (and therefore
    ``catalog_role``) classifies as admin."""
    return _agent_bearer("admin", "manager")


# ── Registry entry fixtures ──────────────────────────────────────────


def _resource_entry(name: str, visibility, prefix: str, render=None):
    from agent_mcp.core.registry import RegistryEntry
    from agent_mcp.resources import ResourceReader

    return RegistryEntry(
        name=name,
        visibility=visibility,
        meta=ResourceReader(
            uri_prefix=prefix,
            description=f"{name} (test)",
            mime_type="application/json",
            render=render or (lambda agent_id: "{}"),
        ),
    )


def _read_request(principal, entry, target: str | None, caller=None):
    from agent_mcp.core.access import Request

    return Request(
        principal=principal,
        surface="resources",
        verb="read",
        entry=entry,
        target_scope=target,
        caller_scope=caller,
    )


# ── R21-F4: admin cross-agent read, now through decide() ─────────────


@pytest.mark.parametrize(
    "principal_factory",
    [_sysadmin_operator, _forwarding_operator, _legacy_admin_bearer],
    ids=["cookie-sysadmin", "forwarding-operator", "legacy-admin-bearer"],
)
def test_decide_allows_admin_cross_agent_resource_read(principal_factory) -> None:
    """Every admin-tier Principal shape reachable in production may read
    another agent's resource — the R21-F4 property, re-expressed through
    the seam. The two operator shapes carry ``agent_id=None``; the legacy
    bearer carries ``agent_id="admin"``. All three must be admitted by
    the SAME check (``catalog_role``), not by three parallel ones."""
    from agent_mcp.core.access import decide
    from agent_mcp.core.principal_builder import catalog_role

    principal = principal_factory()
    assert catalog_role(principal) == "admin"

    entry = _resource_entry("status", "any", "agent-mcp://status/")
    decision = decide(_read_request(principal, entry, "bob"))

    assert decision.allowed, decision.reason
    assert bool(decision) is True


def test_decide_denies_non_admin_cross_agent_resource_read() -> None:
    """The cross-agent IDOR guard: a worker bearer reading another
    agent's resource is denied, and the denial is classified so the
    surface can map it to its own error code."""
    from agent_mcp.core.access import decide

    entry = _resource_entry("status", "any", "agent-mcp://status/")
    decision = decide(_read_request(_agent_bearer("alice"), entry, "bob"))

    assert not decision.allowed
    assert decision.denial == "out_of_scope"


def test_decide_allows_non_admin_own_scope_read() -> None:
    """A worker reading its OWN resource is admitted."""
    from agent_mcp.core.access import decide

    entry = _resource_entry("status", "any", "agent-mcp://status/")
    decision = decide(_read_request(_agent_bearer("alice"), entry, "alice"))

    assert decision.allowed, decision.reason


def test_decide_denies_caller_with_no_resolvable_scope() -> None:
    """No Principal at all (and no caller scope) → denied as
    unauthenticated, not silently scoped to the URI's agent."""
    from agent_mcp.core.access import decide

    entry = _resource_entry("status", "any", "agent-mcp://status/")
    decision = decide(_read_request(None, entry, "bob"))

    assert not decision.allowed
    assert decision.denial == "unauthenticated"


def test_decide_honours_explicit_caller_scope_over_principal() -> None:
    """``caller_scope`` carries the surface's own resolution of "who is
    the caller" (Resources falls back to ``get_agent_id(token)`` when the
    Principal carries no ``agent_id``)."""
    from agent_mcp.core.access import decide

    entry = _resource_entry("status", "any", "agent-mcp://status/")
    viewer = _forwarding_viewer()
    assert viewer.agent_id is None

    allowed = decide(_read_request(viewer, entry, "alice", caller="alice"))
    denied = decide(_read_request(viewer, entry, "bob", caller="alice"))

    assert allowed.allowed, allowed.reason
    assert not denied.allowed
    assert denied.denial == "out_of_scope"


# ── The gap this closes: visibility on the READ path ─────────────────


def test_decide_denies_non_admin_admin_visibility_entry_own_scope() -> None:
    """THE new property. An ``visibility="admin"`` resource addressed at
    the caller's OWN agent_id: the old ``resolve_agent_id_for_uri``-only
    mechanism admitted this (it only ever compared agent_ids), while
    ``resources/list`` correctly hid the entry. ``decide()`` now denies
    the read too, so guessing the URI buys nothing."""
    from agent_mcp.core.access import decide

    entry = _resource_entry("secret", "admin", _SECRET_PREFIX)
    decision = decide(_read_request(_agent_bearer("alice"), entry, "alice"))

    assert not decision.allowed
    assert decision.denial == "not_visible"


def test_decide_allows_admin_on_admin_visibility_entry() -> None:
    from agent_mcp.core.access import decide

    entry = _resource_entry("secret", "admin", _SECRET_PREFIX)
    decision = decide(_read_request(_sysadmin_operator(), entry, "bob"))

    assert decision.allowed, decision.reason


def test_decide_honours_callable_visibility_policies() -> None:
    """A callable visibility policy (tools' ``worker-if-toggled`` shape)
    is evaluated by the SAME ``resolve_visibility`` the list path uses —
    the seam does not re-implement the sentinel vocabulary."""
    from agent_mcp.core.access import decide

    open_entry = _resource_entry(
        "toggled", lambda role: role == "worker", _SECRET_PREFIX
    )
    shut_entry = _resource_entry(
        "toggled", lambda role: False, _SECRET_PREFIX
    )

    worker = _agent_bearer("alice")
    assert decide(_read_request(worker, open_entry, "alice")).allowed
    shut = decide(_read_request(worker, shut_entry, "alice"))
    assert not shut.allowed
    assert shut.denial == "not_visible"


@pytest.mark.parametrize(
    "principal_factory",
    [
        _sysadmin_operator,
        _forwarding_operator,
        _forwarding_viewer,
        _legacy_admin_bearer,
        lambda: _agent_bearer("alice"),
        lambda: None,
    ],
    ids=[
        "cookie-sysadmin",
        "forwarding-operator",
        "forwarding-viewer",
        "legacy-admin-bearer",
        "worker-bearer",
        "anonymous",
    ],
)
def test_decide_visibility_agrees_with_list_visible(principal_factory) -> None:
    """No drift between LIST and READ: for every Principal shape, whether
    ``decide()`` admits an entry on visibility grounds is exactly
    ``resolve_visibility(entry.visibility, catalog_role(principal))`` —
    the predicate ``Registry.list_visible`` filters on. Verifying this
    equivalence is what makes migrating the read path onto the seam a
    mechanism change rather than a policy change."""
    from agent_mcp.core.access import decide
    from agent_mcp.core.principal_builder import catalog_role
    from agent_mcp.core.registry import resolve_visibility

    principal = principal_factory()
    role = catalog_role(principal)

    for visibility in ("any", "admin"):
        entry = _resource_entry("probe", visibility, _SECRET_PREFIX)
        # target_scope=None isolates the visibility gate from the
        # own-agent scoping gate.
        decision = decide(_read_request(principal, entry, None))
        assert decision.allowed is resolve_visibility(visibility, role), (
            f"visibility={visibility!r} role={role!r}"
        )


def test_decide_without_entry_skips_the_visibility_gate() -> None:
    """``entry=None`` (a surface asking only the scoping question) is a
    legitimate Request shape — it must not deny by default."""
    from agent_mcp.core.access import decide

    decision = decide(_read_request(_agent_bearer("alice"), None, "alice"))
    assert decision.allowed, decision.reason


# ── Wired through the real Resources read path ───────────────────────


@pytest.fixture
def secret_resource():
    """Register a synthetic ``visibility="admin"`` resource on the live
    registry for the duration of one test, then remove it.

    Reaching into ``_entries`` to remove is deliberate: ``Registry`` has
    no unregister verb (production never removes an entry), and the
    alternative — ``clear()`` — would drop the two real resources the
    rest of the suite relies on.
    """
    from agent_mcp.resources import resource_registry

    reads: list = []

    def _render(agent_id: str) -> str:
        reads.append(agent_id)
        return '{"secret": true}'

    entry = _resource_entry("secret", "admin", _SECRET_PREFIX, render=_render)
    resource_registry.register(entry)
    try:
        yield reads
    finally:
        resource_registry._entries.pop("secret", None)


def test_admin_visibility_resource_is_denied_to_worker_on_read(
    secret_resource,
) -> None:
    """End of the wire: ``resources/read`` on an admin-only resource, at
    the worker's OWN agent_id, is refused — and the reader never runs.

    Pre-change this returned the payload: the read path's only gate was
    "does the URI's agent_id match yours?", which it did."""
    from agent_mcp.resources import ResourceReadError, resource_registry

    with pytest.raises(ResourceReadError):
        resource_registry.read(
            f"{_SECRET_PREFIX}alice",
            None,
            principal=_agent_bearer("alice"),
        )
    assert secret_resource == [], "reader must not have been invoked"


def test_admin_visibility_resource_is_served_to_admin(secret_resource) -> None:
    from agent_mcp.resources import resource_registry

    text = resource_registry.read(
        f"{_SECRET_PREFIX}bob", None, principal=_sysadmin_operator()
    )
    assert text == '{"secret": true}'
    assert secret_resource == ["bob"]


def test_visible_resource_read_still_works_for_own_agent(
    secret_resource,
) -> None:
    """Sanity: registering an admin-only resource doesn't disturb the
    ``visibility="any"`` ones a worker legitimately reads."""
    from agent_mcp.resources import resolve_agent_id_for_uri

    resolved = resolve_agent_id_for_uri(
        "agent-mcp://status/alice", None, principal=_agent_bearer("alice")
    )
    assert resolved == "alice"
