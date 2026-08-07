"""SEC: the per-project backend session dep must AUTHORIZE, not just
authenticate.

Finding (owner-authorised defensive hardening): the backend's
``require_operator_session`` dep (``agent_mcp/app/deps.py``) admits any
live operator *session cookie* as ``{"kind": "session", ...}`` after
merely confirming the session is live and the user exists. It never
re-checks (a) project membership for THIS backend's project, nor (b)
the viewer-vs-operator read/mutation split. All project-membership and
role enforcement lives only in the router middleware
(``router/auth_middleware.py``), which gates BEFORE proxying.

So if the backend UDS/TCP is reached directly (co-located process, or a
backend bound to a reachable TCP port), a valid session cookie for
project *A* is admitted as operator-tier on project *B*'s backend — the
same cross-project + viewer→operator escalation class SEC-1 fixed on the
forwarding-header wire, left open on the bare-cookie backstop.

These tests pin the fix at the dep level. The backend learns its own
project name by reverse-mapping its ``MCP_PROJECT_DIR`` against the
router-owned project registry, then re-resolves the caller's role for
THAT project via the same resolver the router uses
(``group_resolver.resolve_user_project_role``) and enforces:

  * cookie for a project the operator is NOT a member of  → 401
  * operator-member cookie                                → admit
  * viewer-member cookie + read (GET)                     → admit
  * viewer-member cookie + mutation (POST)                → 403
  * sysadmin cookie (no explicit membership)              → admit

The normal router-proxied path is unaffected: the router already
gated membership before forwarding the cookie, so the backend re-check
resolves the identical role and admits. When the backend cannot
determine its own project name (ad-hoc / test harness with no matching
registry entry) the dep falls back to the pre-existing
authenticate-only behaviour, so existing deps tests stay green.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


# ── Fake ASGI request ──────────────────────────────────────────────


def _make_request(
    method: str = "GET",
    *,
    session_cookie: str | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` carrying an optional
    ``agent_mcp_session`` cookie. No Authorization header, no query
    token — so ``require_operator_session`` lands on the cookie branch.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    if session_cookie is not None:
        raw_headers.append(
            (b"cookie", f"agent_mcp_session={session_cookie}".encode())
        )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": "/api/agents",
        "raw_path": b"/api/agents",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 80),
        "scheme": "http",
    }

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


# ── Router-DB + registry fixture ───────────────────────────────────


@pytest.fixture
def authz_env(tmp_path, monkeypatch):
    """Wire a tmp router.db + project registry so the backend can
    reverse-map its ``MCP_PROJECT_DIR`` → project name and resolve
    membership.

    Returns a small namespace with the seeded project names + a
    ``session_for`` helper that mints a live session cookie for a
    freshly-created (non-sysadmin) user with the requested membership.
    """
    from agent_mcp.core import globals as _g
    from agent_mcp.router import identity
    from agent_mcp.router import project_registry as _pr

    # Router.db at a tmp path the test owns.
    router_db = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(router_db))
    identity.run_router_migrations_upgrade()

    # This backend serves the "beta" project. MCP_PROJECT_DIR is the
    # workspace; the registry maps name→workspace so the reverse-map
    # resolves "beta".
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

    # No forwarding operator stamped — force the cookie branch.
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


# ── Tests ──────────────────────────────────────────────────────────


async def test_cookie_for_other_project_is_denied(authz_env):
    """A valid session cookie for project *alpha*, presented to
    *beta*'s backend dep, is DENIED — no membership in beta."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project="alpha", role="operator")
    req = _make_request("GET", session_cookie=cookie)

    with pytest.raises(HTTPException) as exc:
        await require_operator_session(req)
    assert exc.value.status_code == 401


async def test_operator_member_is_admitted(authz_env):
    """An operator with membership in beta passes and gets the session
    auth context."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project="beta", role="operator")
    req = _make_request("GET", session_cookie=cookie)

    auth = await require_operator_session(req)
    assert auth["kind"] == "session"
    assert auth["user"]["username"] == "user1"


async def test_viewer_member_can_read(authz_env):
    """A viewer with membership in beta may READ (GET admits)."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project="beta", role="viewer")
    req = _make_request("GET", session_cookie=cookie)

    auth = await require_operator_session(req)
    assert auth["kind"] == "session"


async def test_viewer_member_cannot_mutate(authz_env):
    """A viewer with membership in beta may NOT mutate (POST → 403),
    consistent with the router's operator/viewer split."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project="beta", role="viewer")
    req = _make_request("POST", session_cookie=cookie)

    with pytest.raises(HTTPException) as exc:
        await require_operator_session(req)
    assert exc.value.status_code == 403


async def test_sysadmin_bypasses_membership(authz_env):
    """A sysadmin with no explicit beta membership is admitted (mirrors
    the router's sysadmin bypass)."""
    from agent_mcp.app.deps import require_operator_session

    cookie = authz_env.session_for(project=None, sysadmin=True)
    req = _make_request("POST", session_cookie=cookie)

    auth = await require_operator_session(req)
    assert auth["kind"] == "session"
