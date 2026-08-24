"""Wave 12 PR A — the dashboard operator is confirmed operator tier on
their OWN project, so they are not redacted from it.

Root cause (verified, prancy-napping-pie Wave 12 PR A): since PR #280 the
per-project backend already resolves a cookie caller's ``project_role`` +
``sysadmin`` in ``app/deps._authorize_session_for_project`` — then
DISCARDED them (returned ``None``), while
``composition.is_confirmed_operator_tier`` passed only ``kind`` to the
shared predicate. So a genuine cookie *operator* (kind="session") was
never confirmed and got ``[redacted]`` for their own project's agent
bearer tokens (GET /api/tokens) and the settings-store AoE token
(GET /api/settings-data).

The fix FEEDS (does not reimplement) the shared predicate:

  * ``_authorize_session_for_project`` RETURNS ``(project_role, sysadmin)``
    instead of ``None``;
  * ``require_operator_session`` cookie branch carries them in the auth
    dict (``project_role`` / ``sysadmin`` keys);
  * ``composition.is_confirmed_operator_tier`` forwards them.

Invariants pinned here (prior hardenings that MUST NOT regress):

  * a cookie VIEWER (``project_role == "viewer"``) stays NON-confirmed →
    still redacted;
  * the test-harness path (backend can't name its own project →
    ``project_role`` None) stays NON-confirmed (fail-closed);
  * a signed-forwarding caller stays NON-confirmed at this REST seam
    (its role rides the task-local carrier, not this auth dict);
  * an operator-tier bearer stays confirmed (unchanged).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from agent_mcp.app.rest_principal import RestPrincipal

pytestmark = pytest.mark.asyncio


# ── Fake ASGI request (mirrors test_sec_backend_session_authz) ─────────


def _make_request(
    method: str = "GET",
    *,
    session_cookie: str | None = None,
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    if session_cookie is not None:
        raw_headers.append(
            (b"cookie", f"agent_mcp_session={session_cookie}".encode())
        )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": "/api/tokens",
        "raw_path": b"/api/tokens",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 80),
        "scheme": "http",
        "state": {},
    }

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


# ── Router.db + registry fixture (mirrors test_sec_backend_session_authz)


@pytest.fixture
def authz_env(tmp_path, monkeypatch):
    """Wire a tmp router.db + project registry so the backend can
    reverse-map its ``MCP_PROJECT_DIR`` → project name ("beta") and
    resolve the cookie caller's membership role — the exact conditions
    under which PR A must confirm a genuine operator.
    """
    from agent_mcp.core import globals as _g
    from agent_mcp.router import identity
    from agent_mcp.router import project_registry as _pr

    router_db = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(router_db))
    identity.run_router_migrations_upgrade()

    beta_dir = tmp_path / "beta-workspace"
    beta_dir.mkdir()
    monkeypatch.setenv("MCP_PROJECT_DIR", str(beta_dir))

    registry_file = tmp_path / "projects.local.json"
    registry_file.write_text(
        json.dumps(
            {
                "alpha": {"workspace": str(tmp_path / "alpha-workspace")},
                "beta": {"workspace": str(beta_dir)},
            }
        )
    )
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(registry_file))
    monkeypatch.setattr(_pr, "REGISTRY_PATH", registry_file, raising=False)

    monkeypatch.setattr(_g, "current_operator", None, raising=False)

    # Consume the first-user sysadmin bootstrap so subsequent users are
    # plain non-sysadmin operators with no auto-granted memberships.
    identity.create_user(username="seed-sysadmin", password="pw")

    _counter = {"n": 0}

    def _new_user() -> str:
        _counter["n"] += 1
        return identity.create_user(
            username=f"user{_counter['n']}", password="pw"
        )

    class _Env:
        def session_for(
            self,
            *,
            project: str | None = None,
            role: str = "operator",
            sysadmin: bool = False,
        ) -> str:
            uid = _new_user()
            if sysadmin:
                with identity._connect() as conn:
                    conn.execute(
                        "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                        (uid,),
                    )
            if project is not None:
                with identity._connect() as conn:
                    conn.execute(
                        "INSERT INTO project_membership "
                        "(project_name, user_id, role) VALUES (?, ?, ?)",
                        (project, uid, role),
                    )
            return identity.create_session(uid)

    return _Env()


# ── deps seam: the cookie branch now carries the resolved tier ─────────


async def test_cookie_operator_dep_carries_role_and_confirms(authz_env):
    """A cookie OPERATOR on their own project's backend: the dep returns
    the resolved role/sysadmin AND the shared predicate confirms them."""
    from agent_mcp.app.deps import require_operator_session
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    cookie = authz_env.session_for(project="beta", role="operator")
    auth = await require_operator_session(_make_request("GET", session_cookie=cookie))

    assert auth.kind == "session"
    assert auth.project_role == "operator"
    assert auth.sysadmin is False
    assert is_confirmed_operator_tier(auth) is True


async def test_cookie_viewer_stays_unconfirmed(authz_env):
    """A cookie VIEWER is admitted on a GET but must stay NON-confirmed —
    still redacted (prior hardening; must not regress)."""
    from agent_mcp.app.deps import require_operator_session
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    cookie = authz_env.session_for(project="beta", role="viewer")
    auth = await require_operator_session(_make_request("GET", session_cookie=cookie))

    assert auth.kind == "session"
    assert auth.project_role == "viewer"
    assert is_confirmed_operator_tier(auth) is False


async def test_sysadmin_session_confirms(authz_env):
    """A sysadmin cookie (no explicit beta membership) is confirmed."""
    from agent_mcp.app.deps import require_operator_session
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    cookie = authz_env.session_for(project=None, sysadmin=True)
    auth = await require_operator_session(_make_request("GET", session_cookie=cookie))

    assert auth.kind == "session"
    assert auth.sysadmin is True
    assert is_confirmed_operator_tier(auth) is True


async def test_harness_path_no_project_role_is_unconfirmed(authz_env, monkeypatch):
    """When the backend cannot name its own project (ad-hoc / harness),
    the dep leaves ``project_role`` None → conservatively NON-confirmed
    (fail-closed)."""
    import agent_mcp.app.deps as deps
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    # Force the "backend can't resolve its own project" branch.
    monkeypatch.setattr(deps, "_backend_project_name", lambda: None)

    cookie = authz_env.session_for(project="beta", role="operator")
    auth = await deps.require_operator_session(
        _make_request("GET", session_cookie=cookie)
    )

    assert auth.project_role is None
    assert auth.sysadmin is False
    assert is_confirmed_operator_tier(auth) is False


# ── predicate-level invariants (feed, don't reimplement) ───────────────


async def test_predicate_invariants_unchanged():
    """The shared predicate keeps its prior verdicts for the paths PR A
    does not touch — and treats a session missing the new keys as
    least-privilege (additive-safety)."""
    from agent_mcp.app.routers.composition import is_confirmed_operator_tier

    # operator-tier bearer stays confirmed
    assert is_confirmed_operator_tier(RestPrincipal(kind="operator_bearer")) is True
    # signed-forwarding stays NON-confirmed at this REST seam
    assert is_confirmed_operator_tier(RestPrincipal(kind="forwarding")) is False
    # a session missing the new keys defaults to least-privilege
    assert is_confirmed_operator_tier(RestPrincipal(kind="session")) is False
    assert (
        is_confirmed_operator_tier(
            RestPrincipal(kind="session", project_role=None, sysadmin=False)
        )
        is False
    )


# ── endpoint gate: GET /api/tokens honours the produced auth dict ──────


async def test_tokens_endpoint_operator_sees_tokens_viewer_forbidden(authz_env):
    """End-to-end through the real /api/tokens handler with the auth dict
    ``require_operator_session`` actually produces:

      * cookie OPERATOR  → 200 with the real agent bearer token;
      * cookie VIEWER    → 403 (tokens withheld, still redacted).
    """
    from agent_mcp.app.deps import require_operator_session
    from agent_mcp.app.routers.settings import tokens_api_route
    from agent_mcp.core import globals as g

    sentinel_token = "SENTINEL-AGENT-BEARER-9f31"
    saved = dict(g.active_agents)
    g.active_agents.clear()
    g.active_agents[sentinel_token] = {"agent_id": "worker-1", "status": "active"}
    try:
        op_cookie = authz_env.session_for(project="beta", role="operator")
        op_auth = await require_operator_session(
            _make_request("GET", session_cookie=op_cookie)
        )
        resp = await tokens_api_route(
            _make_request("GET", session_cookie=op_cookie), op_auth
        )
        assert resp.status_code == 200
        assert sentinel_token in resp.body.decode()

        view_cookie = authz_env.session_for(project="beta", role="viewer")
        view_auth = await require_operator_session(
            _make_request("GET", session_cookie=view_cookie)
        )
        resp = await tokens_api_route(
            _make_request("GET", session_cookie=view_cookie), view_auth
        )
        assert resp.status_code == 403
        assert sentinel_token not in resp.body.decode()
    finally:
        g.active_agents.clear()
        g.active_agents.update(saved)


async def test_viewer_mutation_still_403(authz_env):
    """Prior hardening unchanged: a cookie viewer performing a mutation
    (POST) is rejected at the dep with 403 before any handler runs."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project="beta", role="viewer")
    with pytest.raises(HTTPException) as exc:
        await require_operator_session(_make_request("POST", session_cookie=cookie))
    assert exc.value.status_code == 403
