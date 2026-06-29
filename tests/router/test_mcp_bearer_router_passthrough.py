"""F015 — per-agent bearer at ``/agent-mcp/mcp/<project>`` reaches the backend.

Regression for the Wave 7 PR 0 success contract:

    register_agent (via MCP) → operator gets token
        → operator pastes snippet into user's claude .mcp.json
        → claude POSTs to /agent-mcp/mcp/<project> with
          Authorization: Bearer <token>
        → request reaches the backend's tools/* handlers.

Before the fix, the router's ``backend_mcp_handler`` had a
pre-check that called ``_agent_token_map`` to validate the bearer.
``_agent_token_map`` hits the backend's ``GET /api/tokens`` over
the project UDS with NO ``Authorization`` header. PR #203 (Wave 1
of prancy-napping-pie, 2026-06-20) added
``Depends(require_operator_session)`` to that endpoint, so the
unauthenticated probe started 401-ing and the per-agent bearer at
``/mcp/<project>`` was rejected with "invalid or missing agent
bearer token" — silent regression that pre-existing tests didn't
catch because they all pre-seeded ``_agent_token_cache``
(see e.g. ``test_proxy_passthrough.py``).

The fix removes the router-side pre-check: the backend's
``AuthHeaderMiddleware`` is the single source of truth for "is
this bearer live?" via ``token in g.active_agents``. This test
exercises the bearer-at-/mcp path end-to-end against a fake
backend that mirrors the production middleware's contract —
NO router-side cache pre-seed — so a future regression that
re-grows the unauthenticated-probe path fails this test the same
way it would break production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


# ── Fake backend — mirrors AuthHeaderMiddleware's bearer-gate ────────


class _BearerGatedBackend:
    """UDS-bound aiohttp app that gates ``/mcp`` on a bearer allowlist.

    Mirrors the production ``AuthHeaderMiddleware`` contract at
    ``agent_mcp/app/main_app.py:427-431``:

        authenticated = bool(forwarding_operator) or (
            bool(token) and token in _g.active_agents
        )

    Tests pass ``active_tokens`` at construction time; the backend
    202s only when the request carries ``Authorization: Bearer
    <token>`` where ``<token>`` is in the allowlist. The router is
    NOT expected to pre-validate against this set — it must forward
    the bearer verbatim and let the backend decide.
    """

    def __init__(self, active_tokens: set[str]) -> None:
        self.active_tokens = active_tokens
        # Records EVERY request the backend received so the test can
        # assert "the request reached the backend at all" without
        # depending on the response shape.
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._gated)
        return app

    async def _gated(self, req: web.Request) -> web.Response:
        auth = req.headers.get("Authorization", "")
        bearer = ""
        if auth.lower().startswith("bearer "):
            bearer = auth[7:].strip()
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
                "bearer_seen": bearer,
            }
        )
        if not bearer or bearer not in self.active_tokens:
            # Mirrors the production middleware's 401 shape. The
            # exact body / WWW-Authenticate header value is irrelevant
            # here; the contract under test is "router forwarded the
            # request to the backend, the backend made the auth
            # decision".
            return web.Response(
                status=401,
                body=b'{"error":"invalid_bearer"}',
                content_type="application/json",
                headers={"WWW-Authenticate": 'Bearer realm="agent-mcp"'},
            )
        return web.Response(
            body=b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}',
            content_type="application/json",
        )


async def _start_backend_on_uds(
    backend: _BearerGatedBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def bearer_gated_backend(
    router_module, router_env, systemctl_stub,
):
    """Stand up a backend that only admits a specific bearer.

    Pre-registers project ``demo`` with the router and marks its
    systemd unit "active" so ``_ensure`` is a no-op. The bearer in
    the allowlist (``known-agent-token``) is what a successful
    ``register_agent`` would return to the operator; an unrelated
    bearer (``unknown-token``) stands in for a revoked or forged
    credential.

    Critically, the fixture does NOT touch ``_agent_token_cache``.
    The router-side pre-check is gone; the backend gates the bearer.
    """
    name = "demo"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _BearerGatedBackend(active_tokens={"known-agent-token"})
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── Tests ───────────────────────────────────────────────────────────


async def test_known_agent_bearer_reaches_backend_and_returns_200(
    aiohttp_client, router_app, bearer_gated_backend, router_module,
) -> None:
    """A POST to ``/agent-mcp/mcp/demo`` with a bearer the backend
    knows about lands at the backend and returns 200.

    F015 success contract: no router-side cache pre-seed, no
    ``_agent_token_map`` round-trip, just bearer-forwarding through
    to the backend's authoritative gate.
    """
    assert router_module._agent_token_cache.get("demo") is None, (
        "test bug: the router-side bearer pre-check is gone; the cache "
        "must not be pre-seeded for this test or the assertion below "
        "would not be exercising the production code path"
    )

    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer known-agent-token",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 200, await resp.text()

    posts = [r for r in bearer_gated_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"expected exactly one /mcp record reaching the backend; got "
        f"{[(r['method'], r['path']) for r in bearer_gated_backend.records]}"
    )
    rec = posts[0]
    assert rec["bearer_seen"] == "known-agent-token", (
        f"backend did not see the bearer the client sent; got "
        f"{rec['bearer_seen']!r}"
    )
    # The router MUST forward the bearer verbatim — the backend's
    # middleware needs it to look the agent up.
    assert rec["headers"].get("Authorization") == "Bearer known-agent-token"


async def test_unknown_agent_bearer_reaches_backend_and_backend_returns_401(
    aiohttp_client, router_app, bearer_gated_backend,
) -> None:
    """An unknown bearer is rejected by the BACKEND (not the router).

    The router used to short-circuit unknown bearers with its own
    401. After F015 it forwards everything and the backend makes
    the auth decision. Pin that the unknown-bearer 401 comes from
    the backend by asserting the request DID reach the backend
    (a routed 401 implies the request never landed).
    """
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer unknown-token",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 401, await resp.text()
    # Backend's WWW-Authenticate header survives the proxy
    # (so MCP clients see the right realm).
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")

    posts = [r for r in bearer_gated_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        "router short-circuited the unknown bearer instead of forwarding "
        "to the backend — that's the F015 pre-check we deleted. The "
        "backend's AuthHeaderMiddleware is the single source of truth "
        "for bearer validity."
    )
    assert posts[0]["bearer_seen"] == "unknown-token"


async def test_missing_bearer_short_circuits_at_the_router(
    aiohttp_client, router_app, bearer_gated_backend,
) -> None:
    """A request with NEITHER a bearer NOR a cookie is 401'd by the
    router — the bearer/cookie absence path is router-local because
    there's nothing to forward.

    The 401 contract: no record at the backend (the router didn't
    forward), and a ``WWW-Authenticate`` header tells the MCP client
    which realm to authenticate against.
    """
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/demo",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={"Content-Type": "application/json"},
        allow_redirects=False,
    )

    assert resp.status == 401, await resp.text()
    assert bearer_gated_backend.records == [], (
        "router forwarded a no-auth request to the backend; the "
        "no-bearer/no-cookie path must short-circuit at the router"
    )
