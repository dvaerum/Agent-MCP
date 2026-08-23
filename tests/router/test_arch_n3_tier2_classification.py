"""N3 Tier 2 — request classification derived from route-registration
facts instead of hand-maintained literal tuples
(``security-arch-hardening-consolidated.md``, "After Phase 4 — N3
Tier 2").

N3 identified five request-classification questions answered TWICE by
different modules with no shared source of truth. Tier 1 (merged,
Phase 1) did the safe subtraction for question 5 (``_MUTATION_METHODS``)
and deliberately left question 2 (peer-trust) alone. This file covers
the remaining three:

  1. **Is this path public?** ``auth_middleware._UNAUTH_PREFIXES`` vs
     ``setup_wizard._REDIRECT_EXEMPT_PREFIXES``. Live bug: the
     ``/agent-mcp/api/router/health`` entry's comment claims it is
     "exact-prefixed", but the matcher is ``path.startswith(p)`` — an
     unbounded prefix match. A future ``/api/router/health-details``
     route would silently bypass the operator-session gate. Same shape
     as R5-F6 (unbounded prefix fallthrough), fixed in the ROUTING
     table but never in this AUTH-BYPASS allowlist.

  3. **Is this a delivery route?** ``auth_middleware._DELIVERY_RE``
     (tolerates a trailing slash) vs ``router/app.py``'s bare
     ``rest in ("delivery/stream", "delivery/status")``. The two
     disagree on the trailing-slash form and on the reserved ``router``
     segment.

  4. **Which project is this?** ``auth_middleware._project_from_path``
     (raw URL segment, ALIAS-UNAWARE) vs
     ``app.py::_resolve_project_or_alias`` (alias-aware). Live bug:
     during an ADR-0010 rename-with-grace window a genuine project
     member hitting the OLD alias resolves ``role=None`` in the auth
     layer and gets the unknown-project response, even though the proxy
     layer resolves the alias fine. Fail-closed, but a real denial of a
     legitimate operator — and inconsistent with the ``/mcp`` transport,
     which already resolves the alias BEFORE the role check
     (``_forwarding_header_from_cookie(req, real_project_name)``).
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── Helpers (mirror tests/router/test_sec_r3_xtenant_oracle.py) ──────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, password: str = "passwordpassword") -> str:
    """Create a NON-sysadmin user. The first user a router ever sees is
    implicitly sysadmin, and a sysadmin bypasses the project-membership
    gate entirely — which is exactly the seam question 4 lives on — so
    make sure somebody else took the first slot."""
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = (
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
    if is_empty and username != "__test_first_sysadmin":
        identity.create_user(
            username="__test_first_sysadmin",
            password="ignoredsentinelpassword",
        )
    return identity.create_user(username=username, password=password)


def _add_membership(
    user_id: str, project_name: str, *, role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_membership "
            "(project_name, user_id, role) VALUES (?, ?, ?)",
            (project_name, user_id, role),
        )


async def _login(
    client, username: str, password: str = "passwordpassword",
) -> str:
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie on successful login"
    name, _, value = set_cookie.split(";", 1)[0].partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


# ══ Question 1 — is this path public? ════════════════════════════════


@pytest.mark.no_auth_seed_session
async def test_unlisted_route_under_health_prefix_stays_session_gated(
    aiohttp_client, router_app,
) -> None:
    """RED: ``/agent-mcp/api/router/health`` is matched with
    ``path.startswith``, so ANY path that merely begins with it —
    ``health-details``, ``healthcheck-internal`` — skips the
    operator-session gate. Only the registered health route (and its
    trailing-slash alias) may be public.

    Pre-fix an unauthenticated caller reaches the per-project catch-all
    and gets its 404 "unknown project"; post-fix the session gate
    rejects with 401 before any handler runs.
    """
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/health-details",
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )
    assert resp.status == 401, (
        f"expected the operator-session gate to reject an unregistered "
        f"path that merely shares the health route's prefix, got "
        f"{resp.status}: {await resp.text()}"
    )


@pytest.mark.no_auth_seed_session
async def test_registered_health_route_and_its_alias_stay_public(
    aiohttp_client, router_app,
) -> None:
    """Regression guard for the fix above: the REGISTERED public route
    — and the trailing-slash alias ``_add_admin_trailing_slash_aliases``
    derives from it — must still answer unauthenticated."""
    client = await aiohttp_client(router_app)

    for path in (
        "/agent-mcp/api/router/health",
        "/agent-mcp/api/router/health/",
    ):
        resp = await client.get(
            path, headers=_STRICT_ACCEPT, allow_redirects=False,
        )
        assert resp.status == 200, (
            f"{path} must stay public: {resp.status} {await resp.text()}"
        )
        body = await resp.json()
        assert body["service"] == "agent-mcp-router"


async def test_public_paths_are_derived_from_the_routing_table(
    router_app,
) -> None:
    """The auth-bypass allowlist's exact-path half must be DERIVED from
    the registered routes (the ``_add_admin_trailing_slash_aliases``
    idiom), not a hand-written literal — so the trailing-slash alias is
    public for free and a new ``/api/router/...`` route is NOT."""
    from agent_mcp.router import path_policy

    derived = path_policy.public_paths(router_app)
    assert derived == frozenset({
        "/agent-mcp/api/router/health",
        "/agent-mcp/api/router/health/",
    }), derived
    assert not any(
        p.startswith("/agent-mcp/api/router/")
        for p in path_policy.UNAUTH_PREFIXES
    ), (
        "no /api/router/... entry may survive as an unbounded PREFIX in "
        f"the auth-bypass allowlist: {path_policy.UNAUTH_PREFIXES}"
    )


async def test_public_marker_travels_with_the_handler_object() -> None:
    """The derivation must key off the registered HANDLER, not a path
    string — that is what makes every mechanically-derived alias
    (trailing-slash, ADR-0020 root) inherit the marking automatically."""
    from aiohttp import web

    from agent_mcp.router import path_policy

    async def handler(_req):  # pragma: no cover - never invoked
        return web.Response()

    app = web.Application()
    app.router.add_get("/agent-mcp/probe", path_policy.public_route(handler))
    # A second, mechanically-derived registration of the SAME handler.
    app.router.add_get("/agent-mcp/probe/", handler)
    app.router.add_get("/agent-mcp/private", lambda _r: web.Response())

    assert path_policy.public_paths(app) == frozenset({
        "/agent-mcp/probe", "/agent-mcp/probe/",
    })


async def test_root_aliased_public_route_normalises_to_the_mount(
    router_app,
) -> None:
    """ADR-0020 mirrors every route at the host root; the gate keys off
    ``mount.canonical_path``. A root-mounted public route must therefore
    land in the derived set in its ``/agent-mcp`` form (which it does —
    that is the assertion in
    ``test_public_paths_are_derived_from_the_routing_table``) and the
    root form must NOT leak in as a separate bypass entry."""
    from agent_mcp.router import path_policy

    derived = path_policy.public_paths(router_app)
    assert all(p.startswith("/agent-mcp/") for p in derived), derived


async def test_unauth_and_redirect_exempt_are_two_named_policies() -> None:
    """The two prefix tuples answer DIFFERENT questions — auth-bypass vs
    setup-redirect-bypass — and their differences are intentional, not
    drift. They are NOT collapsed into one list; they share a home and a
    matcher, and this test pins the exact delta so a future edit to
    either one has to state its case.

    * ``/agent-mcp/login`` + ``/agent-mcp/logout`` are auth-bypass only:
      on a fresh install (empty users table) an operator visiting the
      login form SHOULD be bounced to ``/setup`` — there is no account
      to log into yet.
    * ``/agent-mcp/api/`` is redirect-exempt only: the whole machine-to-
      machine REST surface must never be 303'd to an HTML wizard, but it
      is emphatically still operator-session gated.
    """
    from agent_mcp.router import path_policy

    unauth = set(path_policy.UNAUTH_PREFIXES)
    redirect_exempt = set(path_policy.REDIRECT_EXEMPT_PREFIXES)

    assert unauth - redirect_exempt == {
        "/agent-mcp/login", "/agent-mcp/logout",
    }, (
        "auth-bypass-only prefixes changed; a fresh install must still "
        "bounce /login and /logout to the setup wizard"
    )
    assert redirect_exempt - unauth == {"/agent-mcp/api/"}, (
        "redirect-exempt-only prefixes changed; the REST surface must "
        "stay redirect-exempt AND session-gated"
    )


async def test_both_middlewares_read_the_shared_policy_module() -> None:
    """One canonical home. Both consumers must be the SAME objects, so
    an edit in one place cannot silently diverge from the other."""
    from agent_mcp.router import auth_middleware, path_policy, setup_wizard

    assert auth_middleware._UNAUTH_PREFIXES is path_policy.UNAUTH_PREFIXES
    assert (
        setup_wizard._REDIRECT_EXEMPT_PREFIXES
        is path_policy.REDIRECT_EXEMPT_PREFIXES
    )


# ══ Question 3 — is this a delivery route? ═══════════════════════════


# (canonical path, project segment, backend ``rest``) triples. The auth
# middleware classifies the PATH; ``backend_api_handler`` classifies the
# (name, rest) pair the aiohttp router already split out. Both must
# agree — that is the whole point of a shared source of truth.
_DELIVERY_TRIPLES = [
    ("/agent-mcp/api/washing/delivery/stream", "washing", "delivery/stream"),
    ("/agent-mcp/api/washing/delivery/status", "washing", "delivery/status"),
    # Trailing slash: ``_DELIVERY_RE`` tolerated it, the app.py tuple
    # did not — the live disagreement.
    ("/agent-mcp/api/washing/delivery/stream/", "washing", "delivery/stream/"),
    ("/agent-mcp/api/washing/delivery/status/", "washing", "delivery/status/"),
    # Reserved router segment: ``_DELIVERY_RE``'s ``[^/]+`` matched it,
    # so the operator-session gate was skipped for a path that is not a
    # project at all.
    ("/agent-mcp/api/router/delivery/stream", "router", "delivery/stream"),
    # Non-delivery neighbours.
    ("/agent-mcp/api/washing/delivery/evil", "washing", "delivery/evil"),
    ("/agent-mcp/api/washing/delivery", "washing", "delivery"),
    ("/agent-mcp/api/washing/tokens", "washing", "tokens"),
    ("/agent-mcp/api/router/health", "router", "health"),
]


@pytest.mark.parametrize("path,name,rest", _DELIVERY_TRIPLES)
async def test_delivery_classification_agrees_across_both_consumers(
    path, name, rest,
) -> None:
    """RED for the trailing-slash and reserved-segment rows: the
    path-shaped matcher and the (name, rest)-shaped matcher must return
    the same answer for every adversarial input."""
    from agent_mcp.router import auth_middleware, path_policy

    assert auth_middleware._path_is_delivery(path) is (
        path_policy.is_delivery(name, rest)
    ), (
        f"delivery classification disagrees for {path!r}: "
        f"auth_middleware={auth_middleware._path_is_delivery(path)} vs "
        f"path_policy.is_delivery({name!r}, {rest!r})="
        f"{path_policy.is_delivery(name, rest)}"
    )


async def test_reserved_router_segment_is_not_a_delivery_route() -> None:
    """``router`` is the ADR-0014 admin segment, never a project — a
    ``delivery/`` path under it must not skip the operator gate."""
    from agent_mcp.router import auth_middleware

    assert (
        auth_middleware._path_is_delivery(
            "/agent-mcp/api/router/delivery/stream"
        )
        is False
    )


async def test_backend_api_handler_uses_the_shared_delivery_policy() -> None:
    """A matcher nothing calls is dead: pin that ``backend_api_handler``
    routes its version-gate exemption through the shared policy rather
    than an inline tuple literal."""
    from agent_mcp.router import app as router_app_module

    src = inspect.getsource(router_app_module.backend_api_handler)
    assert "path_policy" in src, (
        "backend_api_handler must classify delivery routes through the "
        "shared path_policy, not a local tuple"
    )
    assert '"delivery/stream"' not in src, (
        "the inline delivery tuple must be gone from backend_api_handler"
    )


async def test_delivery_trailing_slash_exempt_from_version_gate(
    aiohttp_client, router_app, register_project,
) -> None:
    """RED: ``POST .../delivery/status/`` skipped the operator gate (the
    regex tolerates the slash) but NOT the Accept-version gate (the
    tuple did not) — so it 406'd. Both gates must now agree."""
    register_project("delta")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/delta/delivery/status/",
        json={"status": "idle"},
        allow_redirects=False,
    )
    body = await resp.text()
    assert resp.status != 406, (
        f"the trailing-slash delivery form cleared the operator gate but "
        f"tripped the version gate: {body}"
    )
    assert resp.status >= 500, f"expected to reach the down backend: {resp.status} {body}"


@pytest.mark.no_auth_seed_session
async def test_router_segment_delivery_path_is_session_gated(
    aiohttp_client, router_app,
) -> None:
    """RED: ``/agent-mcp/api/router/delivery/stream`` matched
    ``_DELIVERY_RE``'s ``[^/]+`` project group and skipped the
    operator-session gate for an unauthenticated caller."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/delivery/stream",
        headers={"Accept": "text/event-stream"},
        allow_redirects=False,
    )
    assert resp.status == 401, (
        f"expected the operator-session gate to reject the reserved "
        f"router segment, got {resp.status}: {await resp.text()}"
    )


# ══ Question 4 — which project is this? ══════════════════════════════


@pytest.mark.no_auth_seed_session
async def test_member_reaches_project_through_grace_window_alias(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """RED: ADR-0010 rename-with-grace. A genuine member of the REAL
    project hitting the OLD alias got the unknown-project response,
    because the auth layer resolved ``project_membership`` against the
    raw URL segment while the proxy layer resolved the alias.

    Post-fix the request clears the gate and reaches the proxy (which
    5xx's here because no backend is spawned in unit tests) — the same
    outcome a request to the real name gets.
    """
    register_project("n3-new")
    router_module._REGISTRY.add_alias("n3-new", "n3-old", grace_days=7)
    alice = _seed_user("alice-n3")
    _add_membership(alice, "n3-new")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice-n3")

    resp = await client.get(
        "/agent-mcp/api/n3-old/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    body = await resp.text()
    assert resp.status != 404, (
        "a member of the renamed project was denied through its own "
        f"grace-window alias: {resp.status} {body}"
    )
    assert resp.status >= 500, (
        f"expected to reach the down backend: {resp.status} {body}"
    )


@pytest.mark.no_auth_seed_session
async def test_non_member_on_alias_is_still_denied(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """No widening: alias-awareness must restore access for MEMBERS
    only. A non-member hitting the alias keeps getting the same
    unknown-project response a nonexistent slug yields (PF-1's
    cross-tenant indistinguishability)."""
    register_project("n3-new")
    router_module._REGISTRY.add_alias("n3-new", "n3-old", grace_days=7)
    _seed_user("mallory-n3")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "mallory-n3")

    aliased = await client.get(
        "/agent-mcp/api/n3-old/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    aliased_body = await aliased.text()
    ghost = await client.get(
        "/agent-mcp/api/n3-ghost/agents",
        headers=_STRICT_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert aliased.status == ghost.status == 404, aliased_body
    assert aliased_body == await ghost.text()
    assert "n3-new" not in aliased_body


@pytest.mark.no_auth_seed_session
async def test_viewer_on_alias_cannot_mutate(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """The viewer/operator mutation split must survive alias resolution
    — a viewer of the real project reaching it via the alias still gets
    403 on a mutation, not a silent promotion."""
    register_project("n3-new")
    router_module._REGISTRY.add_alias("n3-new", "n3-old", grace_days=7)
    viewer = _seed_user("viewer-n3")
    _add_membership(viewer, "n3-new", role="viewer")

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "viewer-n3")

    resp = await client.post(
        "/agent-mcp/api/n3-old/agents",
        headers={**_STRICT_ACCEPT, "Content-Type": "application/json"},
        data="{}",
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    assert resp.status == 403, (
        f"viewer must not gain mutation rights via the alias URL: "
        f"{resp.status} {await resp.text()}"
    )


async def test_project_resolution_is_alias_aware(
    router_app, register_project, router_module,
) -> None:
    """The auth layer's project identity must be the SAME real project
    name the proxy resolves — one answer to "which project is this?",
    not two."""
    from agent_mcp.router import app as router_app_module
    from agent_mcp.router import auth_middleware

    register_project("n3-new")
    router_module._REGISTRY.add_alias("n3-new", "n3-old", grace_days=7)

    segment, real = auth_middleware._resolved_project_from_path(
        "/agent-mcp/api/n3-old/agents"
    )
    assert segment == "n3-old"
    assert real == "n3-new"
    assert real == router_app_module._resolve_project_or_alias("n3-old")[0]

    # A real (non-aliased) project resolves to itself; an unknown slug
    # resolves to None so the membership gate is skipped and the handler
    # emits its own 404 (no project-existence oracle).
    assert auth_middleware._resolved_project_from_path(
        "/agent-mcp/app/n3-new/"
    ) == ("n3-new", "n3-new")
    assert auth_middleware._resolved_project_from_path(
        "/agent-mcp/api/n3-ghost/agents"
    ) == ("n3-ghost", None)
    assert auth_middleware._resolved_project_from_path(
        "/agent-mcp/api/router/projects"
    ) == (None, None)
