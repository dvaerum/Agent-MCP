"""arch-deepening R4 #3 — resolve the operator tier once per request.

Before this change, ``router/auth_middleware.py``'s
``require_operator_session_middleware`` walked the group-membership
graph for the SAME ``user_id`` four separate times on four
separately-opened ``router.db`` connections:

  1. ``resolve_user_is_sysadmin`` (the sysadmin bit).
  2. ``resolve_user_project_role`` (the mutation gate) — result
     discarded.
  3. ``_safe_resolve_role`` -> ``resolve_user_project_role`` again,
     re-deriving the IDENTICAL value #2 already computed because it
     was never stashed.
  4. ``build_operator_principal`` -> ``resolve_capabilities`` ->
     ``resolve_user_groups`` — a fourth walk, this time for the
     Principal's capability set.

The fix threads ONE resolved ``groups`` set through every consumer
(the sysadmin check, the project-role gate, and capability
resolution) instead of re-walking. ``_safe_resolve_role`` is deleted;
the mutation gate's already-resolved ``role`` is reused for the
Principal directly.

Two tests:

  * ``test_gate_principal_and_forwarding_header_tier_agree`` — the
    INVARIANT that was previously true only by accident (three call
    sites happening to run the same function): the mutation-gate
    tier, the stashed Principal's ``project_role``, and the tier the
    ``/mcp`` cookie path signs into the forwarding header must all
    agree.
  * ``test_single_request_walks_group_graph_exactly_once`` — the
    QUERY-COUNT regression guard: exactly ONE call to the graph-walk
    kernel (``group_resolver._resolve_user_groups_on``) per request,
    pinning the 4->1 collapse and catching a future 5th re-walk.

Both tests call ``require_operator_session_middleware`` (and, for the
invariant test, ``_forwarding_header_from_cookie``) directly rather
than through the full aiohttp ``TestClient`` — the middleware and the
cookie-forwarding helper are ordinary coroutines that only need a
``web.Request`` (built via ``aiohttp.test_utils.make_mocked_request``)
and a router.db seeded through the real ``identity``/``group_resolver``
modules, so a full app + TestServer round-trip buys nothing here.
"""

from __future__ import annotations

import os
import secrets as _secrets

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

pytestmark = [pytest.mark.asyncio, pytest.mark.no_auth_seed_session]


# ── Helpers ─────────────────────────────────────────────────────────


def _setup_project_and_operator(
    router_module, router_env, *, project: str, role: str,
):
    """Register ``project`` and create a non-sysadmin user with ``role``.

    A throwaway first user consumes the "first user on an empty table
    is auto-promoted to sysadmin + all-project-membership" bootstrap
    invariant (``identity.create_user``'s ``was_empty`` branch) so the
    SECOND user (the one under test) gets exactly the ``role`` we
    grant it — not sysadmin, not membership-by-bootstrap.
    """
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    router_module._REGISTRY.register(project, str(router_env.root / "ws"))

    identity.create_user(username="sentinel0", password="pw0")
    user_id = identity.create_user(
        username=f"op-{role}", password="pw", bootstrap_sysadmin=False,
    )
    identity.insert_project_membership(project, user_id=user_id, role=role)
    session_id = identity.create_session(user_id)
    return user_id, session_id


def _cookie_headers(session_id: str, **extra: str) -> dict[str, str]:
    return {"Cookie": f"agent_mcp_session={session_id}", **extra}


async def _handler_capturing(request: web.Request, sink: dict) -> web.Response:
    sink["user"] = request.get("user")
    sink["principal"] = request.get("principal")
    sink["is_sysadmin"] = request.get("is_sysadmin")
    return web.Response(text="ok")


async def _start_dummy_backend_on_uds(name: str) -> web.AppRunner:
    """Bind a no-op aiohttp app on the project's backend UDS.

    ``_forwarding_header_from_cookie`` calls ``_ensure`` before signing
    a header, which polls for the socket FILE to exist (mirrors
    production's systemd-spawned backend). We don't need the backend
    to answer anything for this test — just for the socket to be
    there so the poll succeeds immediately instead of timing out.
    Mirrors ``test_forwarding_header_signing.py``'s ``wave2_backend``
    fixture, trimmed to the bare minimum this test needs.
    """
    from agent_mcp.router import project_orchestrator as _po

    sock_path = _po._sock_path(name, "backend")
    sock_path.unlink(missing_ok=True)
    app = web.Application()

    async def _ok(_req: web.Request) -> web.Response:
        return web.Response(status=200)

    app.router.add_route("*", "/{tail:.*}", _ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


# ── Invariant: gate tier == Principal.project_role == signed /mcp tier ──


@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_gate_principal_and_forwarding_header_tier_agree(
    router_module, router_env, systemctl_stub, role: str,
) -> None:
    """The tier the mutation gate enforces, the tier stashed on the
    Principal, and the tier signed into the ``/mcp`` forwarding header
    must be the SAME value for the SAME (user, project) — today they
    only agree because three independent call sites happen to run the
    identical resolver; nothing asserted it.

    Uses a GET (a read) so both ``operator`` and ``viewer`` are
    admitted by the mutation gate — the point here is tier agreement,
    not the mutation/read split (covered elsewhere).
    """
    from agent_mcp.app import forwarding_header as _fh
    from agent_mcp.router import project_orchestrator as _po
    from agent_mcp.router.app import _forwarding_header_from_cookie
    from agent_mcp.router.auth_middleware import (
        require_operator_session_middleware,
    )

    name = "proj-agree"
    _user_id, session_id = _setup_project_and_operator(
        router_module, router_env, project=name, role=role,
    )

    # ── (a) + (b): the mutation-gate tier and the stashed Principal ──
    captured: dict = {}
    req = make_mocked_request(
        "GET",
        f"/agent-mcp/api/{name}/tasks",
        headers=_cookie_headers(
            session_id, Accept="application/vnd.agent-mcp.v1+json",
        ),
    )
    resp = await require_operator_session_middleware(
        req, lambda r: _handler_capturing(r, captured),
    )
    assert resp.status == 200, (
        f"a {role}-tier operator's GET must be admitted; got {resp.status}"
    )
    principal = captured["principal"]
    assert principal is not None, "Principal must be stashed on the request"
    gate_and_principal_tier = principal.project_role
    assert gate_and_principal_tier == role

    # ── (c): the /mcp cookie path's signed forwarding-header tier ──
    hmac_key = _secrets.token_bytes(32)
    key_path = _po._forwarding_hmac_path(name)
    key_path.write_bytes(hmac_key)
    os.chmod(key_path, 0o600)
    assert _po.ensure_forwarding_hmac_key(name) == hmac_key
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    runner = await _start_dummy_backend_on_uds(name)
    try:
        mcp_req = make_mocked_request(
            "POST",
            f"/agent-mcp/mcp/{name}",
            headers=_cookie_headers(session_id),
        )
        header = await _forwarding_header_from_cookie(mcp_req, name)
    finally:
        await runner.cleanup()
    assert header is not None, "cookie path must sign a forwarding header"
    _header_name, header_value = header
    verified = _fh.verify(header_value, hmac_key)
    assert verified is not None, "signed header must verify against the key"
    _operator_id, signed_tier = verified

    # THE invariant.
    assert gate_and_principal_tier == principal.project_role == signed_tier


# ── Query-count: exactly ONE group-graph walk per request ──────────


async def test_single_request_walks_group_graph_exactly_once(
    router_module, router_env, monkeypatch,
) -> None:
    """A single non-sysadmin project-scoped mutation request must walk
    ``group_membership`` exactly ONCE — pinning the 4->1 collapse and
    catching a future re-introduction of a redundant walk.

    Counts calls to ``group_resolver._resolve_user_groups_on``, the
    ONE kernel every walk (public ``resolve_user_groups``, the
    sysadmin check, and the project-role resolver) funnels through —
    counting the public ``resolve_user_groups`` wrapper alone would
    miss the pre-fix duplication, since the sysadmin/role resolvers
    called the private kernel directly rather than the wrapper.
    """
    from agent_mcp.router import group_resolver, identity
    from agent_mcp.router.auth_middleware import (
        require_operator_session_middleware,
    )

    name = "proj-count"
    _user_id, session_id = _setup_project_and_operator(
        router_module, router_env, project=name, role="operator",
    )
    identity.get_session(session_id)  # sanity: session resolvable

    calls: list[tuple] = []
    real_kernel = group_resolver._resolve_user_groups_on

    def _counting_kernel(conn, user_id):
        calls.append((conn, user_id))
        return real_kernel(conn, user_id)

    monkeypatch.setattr(group_resolver, "_resolve_user_groups_on", _counting_kernel)

    captured: dict = {}
    req = make_mocked_request(
        "POST",
        f"/agent-mcp/api/{name}/tasks",
        headers=_cookie_headers(
            session_id, Accept="application/vnd.agent-mcp.v1+json",
        ),
    )
    resp = await require_operator_session_middleware(
        req, lambda r: _handler_capturing(r, captured),
    )
    assert resp.status == 200, (
        f"operator-tier POST must be admitted; got {resp.status}"
    )
    assert captured["principal"] is not None

    assert len(calls) == 1, (
        f"expected exactly ONE group-membership-graph walk for this "
        f"request; got {len(calls)} (arch-r4 #3 4->1 collapse regressed)"
    )
