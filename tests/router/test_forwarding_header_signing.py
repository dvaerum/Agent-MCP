"""End-to-end regression for F015 — cookie→forwarding-header chain.

History
-------

This file was originally written for PR #207's F015 patch (cookie →
system-token-bearer injection). retire-system-token Wave 2
(2026-06-23) rewrote it for the signed-forwarding-header path: the
router no longer translates a cookie into a system bearer; it signs
a per-request ``X-Agent-MCP-Forwarded-Operator`` header that the
backend's Wave 1 ``AuthHeaderMiddleware`` verifies against the
per-project HMAC key.

Architecture under test
-----------------------

1.  Per-project HMAC key lives at
    ``<sock_dir>/<name>/forwarding_hmac``. F015 v4 moved key
    generation from the router into the systemd unit's
    ExecStartPre (see ``nix/module.nix``). The router only READS;
    ``project_orchestrator.ensure_forwarding_hmac_key`` is a
    cache-front for that read. The fixture writes the file directly
    (simulating what the unit's ExecStartPre does in production),
    then lets the router pick it up through the normal reader path.
2.  Cookie-authenticated request lands on ``backend_mcp_handler``.
    ``_forwarding_header_from_cookie`` validates the cookie, checks
    project membership, then signs the header with the per-project
    key via ``agent_mcp.app.forwarding_header.sign``.
3.  Header travels via ``_proxy_to_backend(inject_header=…)`` to the
    backend's UDS.
4.  The fake backend in this test verifies the HMAC against the same
    key the router wrote — i.e. exercises the same code path
    ``AuthHeaderMiddleware`` runs in production. A permissive fake
    backend (PR #207's mistake) would silently accept tampered
    headers and miss the load-bearing check.

The pre-Wave-2 cookie→bearer path is deleted; the corresponding
``Authorization: Bearer <system_token>`` assertion is gone. The new
contract is "the forwarding header verifies", not "an admin bearer
appears".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


# ── Helpers ─────────────────────────────────────────────────────────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, password: str = "pw") -> str:
    return _identity_module().create_user(
        username=username, password=password,
    )


async def _login(client, username: str, password: str = "pw") -> str:
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie
    name_val = set_cookie.split(";", 1)[0]
    _, _, value = name_val.partition("=")
    return value.strip()


# ── Fake backend ────────────────────────────────────────────────────


class _Wave2Backend:
    """UDS-bound aiohttp app that VERIFIES the forwarding-header HMAC.

    This is the load-bearing detail: a permissive fake backend (which
    would 200 on any request) would mask the entire Wave 2 contract.
    This backend mirrors ``AuthHeaderMiddleware``'s verify path: 401
    on missing/invalid header, 200 on a header that verifies against
    the per-project key it was constructed with.
    """

    def __init__(self, hmac_key: bytes) -> None:
        self.hmac_key = hmac_key
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/tokens", self._tokens)
        app.router.add_route("*", "/{tail:.*}", self._gated)
        return app

    async def _tokens(self, req: web.Request) -> web.Response:
        # Wave 3 (PR #205) shape — admin_token deliberately absent.
        return web.json_response(
            {
                "agent_tokens": [
                    {"agent_id": "worker-1", "token": "agent-token-1"},
                ],
            },
        )

    async def _gated(self, req: web.Request) -> web.Response:
        from agent_mcp.app import forwarding_header as _fh

        raw = req.headers.get(_fh.HEADER_NAME)
        operator_id = None
        role = None
        if raw is not None:
            verified = _fh.verify(raw, self.hmac_key)
            if verified is not None:
                operator_id, role = verified
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
                "operator_id": operator_id,
                "role": role,
            },
        )
        if operator_id is None:
            # Mirrors the production middleware: a missing/invalid
            # forwarding header on a cookie path is 401, not 200. We
            # don't accept a bearer here because the only auth mode
            # under test is the cookie→forwarding-header path.
            return web.Response(status=401, body=b"forwarding header required")
        return web.Response(
            body=b'{"ok":true}', content_type="application/json",
        )


async def _start_backend_on_uds(
    backend: _Wave2Backend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def wave2_backend(router_module, router_env, systemctl_stub):
    """Stand up the Wave-2 backend for project ``demo``.

    Pre-populates the per-project HMAC key on disk the way the
    systemd unit's ExecStartPre would in production (F015 v4), then
    lets the router's reader pick it up off disk.
    """
    import os
    import secrets

    from agent_mcp.router import project_orchestrator as _po

    name = "demo"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"

    # Mirror the production lifecycle: systemd unit's ExecStartPre
    # writes the HMAC key BEFORE the launcher starts the backend.
    # This test bypasses systemd and writes the file directly.
    hmac_key = secrets.token_bytes(32)
    key_path = _po._forwarding_hmac_path(name)
    key_path.write_bytes(hmac_key)
    os.chmod(key_path, 0o600)
    # Warm the router's reader cache off the on-disk file.
    assert _po.ensure_forwarding_hmac_key(name) == hmac_key

    backend = _Wave2Backend(hmac_key=hmac_key)
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()
        # Wipe in-memory state so the next test starts clean.
        _po.forwarding_hmac_keys.pop(name, None)


# ── Tests ──────────────────────────────────────────────────────────


async def test_cookie_only_request_forwards_signed_header(
    aiohttp_client, router_app, wave2_backend, router_module,
) -> None:
    """A cookie-authenticated POST to ``/agent-mcp/mcp/demo`` must reach
    the backend with a verified ``X-Agent-MCP-Forwarded-Operator`` header.

    This is the F015 fix and the new Wave 2 contract rolled into one:

      * cookie validates against ``sessions`` → operator_id resolved;
      * project membership check passes;
      * router signs with the per-project HMAC key (Wave 2 launcher
        plumbing wrote the key file before this request landed);
      * backend's verify path returns the operator_id → 200.

    We do NOT pre-seed ``_agent_token_cache``. The cache must stay
    out of the cookie path entirely — the system-token-via-cache
    short-circuit that PR #207 relied on no longer exists.
    """
    from agent_mcp.app import forwarding_header as _fh

    assert router_module._agent_token_cache.get("demo") is None, (
        "test bug: _agent_token_cache should not be pre-seeded; the "
        "cookie path no longer consults this cache"
    )

    # Stand the app up FIRST so the sentinel operator (env-var
    # bootstrap inside ``_init_router_identity_on_startup``) becomes
    # the first user — otherwise alice would be the first user and
    # ``create_user`` would auto-grant her sysadmin + membership in
    # every registered project, masking the membership check.
    client = await aiohttp_client(router_app)
    alice_id = _seed_user("alice")
    _identity_module().add_project_membership(alice_id, "demo")
    alice_cookie = await _login(client, "alice")

    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()

    posts = [r for r in wave2_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"expected exactly one /mcp record reaching the backend, got "
        f"{[(r['method'], r['path']) for r in wave2_backend.records]}"
    )
    rec = posts[0]
    assert rec["operator_id"] == alice_id, (
        f"backend verified operator_id={rec['operator_id']!r}; expected "
        f"the dashboard-logged-in user's id {alice_id!r}"
    )
    # SEC-1: the router signs alice's REAL per-project role.
    # ``add_project_membership`` grants the default ``operator`` role.
    assert rec["role"] == "operator", (
        f"backend verified role={rec['role']!r}; expected 'operator' "
        f"(the default project_membership.role granted to alice)"
    )
    # No legacy bearer should be injected — the cookie path is now
    # bearer-free.
    assert "Authorization" not in rec["headers"], (
        f"cookie path must not inject Authorization; got "
        f"{rec['headers'].get('Authorization')!r}"
    )
    # And the header value is well-formed under the documented contract:
    # SEC-1 four fields (operator_id.role.expiry.hmac).
    raw = rec["headers"][_fh.HEADER_NAME]
    assert raw.count(".") == 3, (
        f"forwarding header should be four dot-separated fields, got "
        f"{raw!r}"
    )


async def test_cookie_with_non_member_operator_is_rejected(
    aiohttp_client, router_app, wave2_backend, router_module,
) -> None:
    """An operator whose cookie is valid but who has no membership in
    the project must NOT have a forwarding header signed for them.

    The 401 here is the router's, NOT the backend's: the cookie path
    refuses to sign before forwarding, so the backend never sees the
    request. The proof is the absence of a /mcp record on the fake
    backend.
    """
    # Start the app first so the sentinel is the first user (and
    # auto-sysadmin). Alice is then a regular non-member.
    client = await aiohttp_client(router_app)
    _seed_user("alice")  # second user → not auto-promoted
    alice_cookie = await _login(client, "alice")

    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()

    posts = [r for r in wave2_backend.records if r["path"] == "/mcp"]
    assert posts == [], (
        f"non-member cookie must short-circuit at the router and never "
        f"reach the backend; got {posts!r}"
    )


async def test_no_cookie_and_no_bearer_is_rejected(
    aiohttp_client, router_app, wave2_backend,
) -> None:
    """No auth → 401 at the router, backend untouched.

    Mirrors the pre-Wave-2 behaviour; verifies the rewrite didn't
    accidentally open the bare-request path.
    """
    client = await aiohttp_client(router_app)
    _seed_user("alice")  # Seed a user so the empty-users middleware no-ops.
    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        allow_redirects=False,
    )
    assert resp.status == 401, await resp.text()
    assert wave2_backend.records == [], (
        "unauthenticated request must not reach the backend"
    )


async def test_per_agent_bearer_path_does_not_inject_forwarding_header(
    aiohttp_client, router_app, wave2_backend, router_module,
) -> None:
    """The agent-side bearer path must NOT attach the forwarding header.

    Two reasons this matters:

      1. The bearer path is the per-principal credential — the
         operator_id stamp in the forwarding header would be a
         fabricated identity, breaking audit attribution.
      2. The backend's middleware rejects a present-but-invalid
         forwarding header (Wave 1 defaults-secure behaviour); if the
         router accidentally signed one with a stale key on the
         bearer path, every legitimate agent request would 401.

    Verifies the cookie path's plumbing is conditional on cookie auth.
    """
    # Pre-seed the agent token map so we don't poke the backend's
    # /api/tokens during this test.
    router_module._agent_token_cache["demo"] = (
        9.9e18, {"agent-token-1": "worker-1"},
    )
    client = await aiohttp_client(router_app)
    _seed_user("alice")  # Seed user → empty-users middleware no-ops.
    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={
            "Authorization": "Bearer agent-token-1",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        allow_redirects=False,
    )
    # Backend rejects without forwarding header (it's the cookie-only
    # fake), so the contract under test is: the router forwarded the
    # bearer-only request through, which the fake backend then 401s.
    assert resp.status == 401, await resp.text()

    posts = [r for r in wave2_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"bearer path should reach the backend; got {posts!r}"
    )
    from agent_mcp.app import forwarding_header as _fh
    rec = posts[0]
    assert _fh.HEADER_NAME not in rec["headers"], (
        f"bearer path injected a forwarding header: "
        f"{rec['headers'].get(_fh.HEADER_NAME)!r}; the cookie path's "
        f"plumbing must not fire when the caller authenticates via "
        f"a per-agent bearer"
    )
    # The bearer itself does pass through (so the backend's
    # AuthHeaderMiddleware can read it from Authorization).
    assert rec["headers"].get("Authorization") == "Bearer agent-token-1"


async def test_client_supplied_forwarding_header_is_stripped_on_bearer_path(
    aiohttp_client, router_app, wave2_backend, router_module,
) -> None:
    """Defense-in-depth: a client attaching its own
    ``X-Agent-MCP-Forwarded-Operator`` header to a bearer-authenticated
    request must NOT have that header reach the backend.

    Two failure modes the strip defends against:

      1. DoS — the backend's ``AuthHeaderMiddleware`` rejects a
         present-but-invalid header (defaults-secure Wave 1 behaviour).
         A client attaching a garbage header to every bearer request
         would 401 the entire agent-side path.
      2. Key-compromise amplifier — if the per-project HMAC key ever
         leaked, an attacker could forge a forwarding header and
         attach it to a bearer request to re-attribute the operator
         identity stamped on ``g.current_operator``.

    The strip is unconditional in ``_proxy_to_backend`` (the router
    is the only authoritative signer); this test pins it.
    """
    from agent_mcp.app import forwarding_header as _fh

    router_module._agent_token_cache["demo"] = (
        9.9e18, {"agent-token-1": "worker-1"},
    )
    client = await aiohttp_client(router_app)
    _seed_user("alice")

    # Attach BOTH a valid bearer AND a forged forwarding header.
    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={
            "Authorization": "Bearer agent-token-1",
            _fh.HEADER_NAME: "attacker.99999999999.deadbeef" * 4,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        allow_redirects=False,
    )
    # The fake backend rejects without forwarding header (it's cookie-
    # only), so this resp 401s. The contract under test is the upstream
    # request shape.
    assert resp.status == 401, await resp.text()

    posts = [r for r in wave2_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"bearer path should reach the backend; got {posts!r}"
    )
    rec = posts[0]
    assert _fh.HEADER_NAME not in rec["headers"], (
        f"forged forwarding header from the client survived proxying: "
        f"{rec['headers'].get(_fh.HEADER_NAME)!r}. The router must "
        f"strip this unconditionally; the backend's defense relies on "
        f"the router being the only authoritative signer."
    )
    assert rec["headers"].get("Authorization") == "Bearer agent-token-1"
