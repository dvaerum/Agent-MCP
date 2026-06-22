"""End-to-end regression for F015 — cookie→bearer injection chain.

Failure F015 surfaced after Wave 3 (PR #205) removed ``admin_token``
from the backend's ``GET /api/tokens`` response. The router's Wave 2
cookie-auth path in ``backend_mcp_handler`` calls
``_admin_bearer_from_cookie`` → ``_agent_token_map`` to obtain the
project's system token; the latter sourced it from the
``admin_token`` field. Wave 3 dropped the field → no system token in
the map → ``_admin_bearer_from_cookie`` returns None → request 401s.

The existing test at ``test_dashboard_session_auth.py:241-305``
pre-seeds ``_agent_token_cache`` directly with ``{"injected-token":
"Admin"}`` — which masks this exact bug because the cache shortcut
in ``_agent_token_map`` returns before hitting the backend. THAT
test still verifies a legitimate scenario (cookie middleware passes
membership → handler proceeds) so we leave it alone; THIS test
exercises the integrated production chain *without* pre-seeding the
cache so the fix path (source the per-project system token from the
router's orchestrator-state channel, not from ``/api/tokens``) is
actually exercised.

Architecture under test:

  1. Backend writes its system token to
     ``<sock_dir>/<name>/system_token`` at spawn time via
     ``--system-token-out``.
  2. Router reads the same path inside ``_agent_token_map`` to
     populate the ``"Admin"`` mapping entry.
  3. ``_admin_bearer_from_cookie`` finds it under ``"Admin"`` and
     ``backend_mcp_handler`` injects it as ``Authorization: Bearer``
     upstream.
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


class _Wave3Backend:
    """UDS-bound aiohttp app serving the Wave-3 ``/api/tokens`` shape.

    Critically, the response carries ONLY ``agent_tokens``. No
    ``admin_token`` field — that's the Wave-3 change F015 stumbled
    over.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/tokens", self._tokens)
        app.router.add_route("*", "/{tail:.*}", self._record)
        return app

    async def _tokens(self, req: web.Request) -> web.Response:
        # Wave 3 (PR #205) shape — admin_token is deliberately absent.
        return web.json_response(
            {
                "agent_tokens": [
                    {"agent_id": "worker-1", "token": "agent-token-1"},
                ],
            },
        )

    async def _record(self, req: web.Request) -> web.Response:
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
            },
        )
        return web.Response(body=b'{"ok":true}', content_type="application/json")


async def _start_backend_on_uds(
    backend: _Wave3Backend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def wave3_backend(router_module, router_env, systemctl_stub):
    """Stand up the Wave-3 backend for project ``demo``.

    Writes the system token to the orchestrator-state channel
    (``<sock_dir>/<name>/system_token``) the way the launcher will
    once the backend is spawned with ``--system-token-out``. The
    router reads from that channel; this fixture is the production
    proxy for "backend has written its token".
    """
    name = "demo"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _Wave3Backend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    # Materialise the orchestrator-state channel the backend's
    # ``--system-token-out`` flag would write at spawn time.
    token_path = router_env.sock_dir / name / "system_token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("project-system-token-XYZ\n")
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── Test ────────────────────────────────────────────────────────────


async def test_cookie_only_request_injects_per_project_system_token(
    aiohttp_client, router_app, wave3_backend, router_module,
) -> None:
    """A cookie-authenticated POST to ``/agent-mcp/mcp/demo`` must reach
    the backend with ``Authorization: Bearer <project_system_token>``.

    F015 regression: with Wave 3's ``/api/tokens`` shape the router
    used to fail to resolve the cookie path because it sourced the
    system token from the backend response's now-removed
    ``admin_token`` field. The fix: source it from the
    orchestrator-state channel (the file written by
    ``--system-token-out`` at spawn time).

    Importantly, we do NOT pre-seed ``_agent_token_cache``. The cache
    must be populated via the production code path so the bug is
    actually re-exercised — the existing
    ``test_dashboard_session_auth.py::test_mcp_route_with_operator_cookie_reaches_handler``
    pre-seeds the cache and therefore CANNOT see F015.
    """
    # Sanity guard: the cache must be empty when we arrive, otherwise
    # this test is also masking the bug.
    assert router_module._agent_token_cache.get("demo") is None, (
        "test bug: _agent_token_cache should not be pre-seeded for F015 "
        "regression"
    )

    alice_id = _seed_user("alice")
    _identity_module().add_project_membership(alice_id, "demo")
    client = await aiohttp_client(router_app)
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

    # Cookie path is supposed to reach the backend (status 200 from
    # the fake backend). Before the fix this is a 401 from
    # ``_unauthorized()`` because ``_admin_bearer_from_cookie`` cannot
    # find the system token in the map.
    assert resp.status == 200, await resp.text()

    # Find the POST /mcp record (the backend ALSO served /api/tokens
    # during the resolution; we want the proxied initialize).
    posts = [r for r in wave3_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"expected exactly one /mcp record reaching the backend, got "
        f"{[(r['method'], r['path']) for r in wave3_backend.records]}"
    )
    rec = posts[0]
    auth = rec["headers"].get("Authorization")
    assert auth == "Bearer project-system-token-XYZ", (
        f"upstream Authorization header was {auth!r}; expected the "
        f"per-project system token from the orchestrator-state channel"
    )
