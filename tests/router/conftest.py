"""Test fixtures for ``agent_mcp.router.app`` + ``…project_registry``.

The router module reads a fistful of environment variables at import
time (``AGENT_MCP_PROJECTS_FILE``, ``AGENT_MCP_SOCK_DIR``, …) and also
``read_text()``s the installer template — so we can't just import the
module from anywhere; we have to set those env vars first and we
have to make the import side-effects test-scoped.

The pattern below: every test that touches the router goes through
``router_app`` (or one of the fixtures that depend on it). That
fixture:

1.  Builds a fresh tmp directory tree (projects file, sock dir,
    dashboard dir, installer template).
2.  Sets all the env vars the router expects.
3.  Drops ``agent_mcp.router.app`` + ``…project_registry`` out of
    ``sys.modules`` so the next import re-executes module-level code
    against the new env.
4.  Imports both modules and patches ``app._systemctl`` to a
    recording stub — the real ``systemctl --user`` call would either
    no-op (returncode 4) or actually start backend systemd units in a
    test environment, neither of which is what we want.
5.  Yields the built aiohttp app.

The ``aiohttp_client`` fixture (from pytest-aiohttp, declared in
``pyproject.toml``'s dev deps) wraps this app in a ``TestClient`` /
``TestServer`` pair on demand.

Phase 0 used a ``_fixtures/`` directory next to this file holding
verbatim copies of the source. Phase 1a (this commit) moved the
source upstream to ``agent_mcp/router/`` — the fixtures directory
and ``sys.path`` shim are gone.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers for the router test fixtures.

      * ``no_seed_operator``: skip the sentinel-operator bootstrap.
        Used by the setup-wizard tests that exercise the empty-users
        state directly.
      * ``no_auth_seed_session``: skip the auto-login of the sentinel
        operator into the aiohttp TestClient's cookie jar (PR D).
        Used by tests that exercise the unauthenticated 401 path
        explicitly.
    """
    config.addinivalue_line(
        "markers",
        "no_seed_operator: do not seed a sentinel operator into router.db",
    )
    config.addinivalue_line(
        "markers",
        "no_auth_seed_session: do not pre-login the sentinel operator "
        "into the aiohttp TestClient's cookie jar",
    )


# ── Env scaffolding ─────────────────────────────────────────────────


@dataclass
class _RouterEnv:
    """Filesystem + env-var layout the router needs at import time."""

    root: Path
    projects_file: Path
    sock_dir: Path
    dashboard_dir: Path
    installer_template: Path
    external_url: str = "https://router.example.test"
    router_port: int = 13370
    idle_sec: int = 14400


def _build_env(tmp_path: Path) -> _RouterEnv:
    """Lay down the directory tree the router expects."""
    projects_file = tmp_path / "projects.local.json"
    sock_dir = tmp_path / "sock"
    sock_dir.mkdir()
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    installer = tmp_path / "installer.sh.in"
    # A minimal installer template — just enough to satisfy the
    # `read_text()` at import time. Substitution is exercised separately.
    installer.write_text(
        "#!/bin/sh\necho url=__AGENT_MCP_MCP_URL__ token=__AGENT_MCP_AGENT_TOKEN__\n"
    )
    return _RouterEnv(
        root=tmp_path,
        projects_file=projects_file,
        sock_dir=sock_dir,
        dashboard_dir=dashboard_dir,
        installer_template=installer,
    )


def _apply_env(monkeypatch: pytest.MonkeyPatch, env: _RouterEnv) -> None:
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(env.projects_file))
    monkeypatch.setenv("AGENT_MCP_SOCK_DIR", str(env.sock_dir))
    monkeypatch.setenv("AGENT_MCP_DASHBOARD_DIR", str(env.dashboard_dir))
    monkeypatch.setenv("AGENT_MCP_EXTERNAL_URL", env.external_url)
    monkeypatch.setenv("AGENT_MCP_INSTALLER_TEMPLATE", str(env.installer_template))
    monkeypatch.setenv("AGENT_MCP_ROUTER_PORT", str(env.router_port))
    monkeypatch.setenv("AGENT_MCP_IDLE_SEC", str(env.idle_sec))
    # Steer the default-workspace-parent (used by __create) into the
    # test root so we never touch ~/.local/share/agent-mcp.
    monkeypatch.setenv(
        "AGENT_MCP_DEFAULT_WORKSPACE", str(env.root / "workspaces")
    )
    # Phase 1 PR B (prancy-napping-pie): the router's startup hook
    # runs Alembic migrations against router.db, defaulting to
    # /var/lib/agent-mcp/router.db. Point it at the test root so we
    # never touch the production path.
    monkeypatch.setenv(
        "AGENT_MCP_ROUTER_DB", str(env.root / "router.db")
    )


# ── systemctl stub ──────────────────────────────────────────────────


@dataclass
class _SystemctlRecorder:
    """Stand-in for ``router._systemctl``. Records every call and lets
    individual tests dictate the active-units list + return codes."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    active_units: set[str] = field(default_factory=set)
    failures: dict[str, int] = field(default_factory=dict)
    # Counter of (verb, unit) pairs for quick assertions.
    counts: Counter = field(default_factory=Counter)

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if len(args) >= 2:
            verb, unit = args[0], args[1]
            self.counts[(verb, unit)] += 1
            if verb == "is-active":
                rc = 0 if unit in self.active_units else 3
                return subprocess.CompletedProcess(
                    args=list(args), returncode=rc, stdout="", stderr="",
                )
            if verb == "start" or verb == "restart":
                self.active_units.add(unit)
            elif verb == "stop":
                self.active_units.discard(unit)
        rc = self.failures.get(args[0] if args else "", 0)
        return subprocess.CompletedProcess(
            args=list(args), returncode=rc, stdout="", stderr="",
        )


# ── Public fixtures ─────────────────────────────────────────────────


@pytest.fixture
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RouterEnv:
    """Build the on-disk layout + set env vars for a fresh router import."""
    env = _build_env(tmp_path)
    _apply_env(monkeypatch, env)
    return env


@pytest.fixture
def systemctl_stub(monkeypatch: pytest.MonkeyPatch) -> _SystemctlRecorder:
    """Recorder for ``router._systemctl`` calls. Use the returned object
    to assert call shape; mutate ``.active_units`` to fake systemd state."""
    return _SystemctlRecorder()


@pytest.fixture
def router_module(
    router_env: _RouterEnv,
    systemctl_stub: _SystemctlRecorder,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    """Import ``agent_mcp.router.app`` with all side-effects test-scoped.

    Each test that requests this fixture gets a freshly re-imported
    module — module-level state (token cache, last_active, ensure
    locks, the global registry handle) is reset by definition.

    Phase 1 PR C (prancy-napping-pie) added an empty-users redirect
    middleware that bounces every ``/agent-mcp/`` request to
    ``/agent-mcp/setup`` until an operator account exists. The legacy
    router tests don't care about identity at all — they test the
    proxy, registry, dashboard, etc. — so we seed a sentinel operator
    via the existing env-var bootstrap path so ``init_router_db``
    (called from ``make_app``'s ``on_startup`` hook) creates one
    automatically and the middleware is a no-op by the time the test
    issues its first request.

    Tests that DO want the empty-users state (the setup-wizard
    tests in particular) opt out with the ``no_seed_operator``
    pytest mark — checked via the request fixture below.
    """
    # Drop any prior copy so module-level env reads run again.
    for mod_name in (
        "agent_mcp.router",
        "agent_mcp.router.app",
        "agent_mcp.router.project_orchestrator",
        "agent_mcp.router.project_registry",
        "agent_mcp.router.identity",
        "agent_mcp.router.login",
        "agent_mcp.router.setup_wizard",
        "agent_mcp.router.migrations_runner",
    ):
        sys.modules.pop(mod_name, None)
    if request.node.get_closest_marker("no_seed_operator") is None:
        monkeypatch.setenv(
            "AGENT_MCP_BOOTSTRAP_USERNAME", "test_sentinel_op"
        )
        monkeypatch.setenv(
            "AGENT_MCP_BOOTSTRAP_PASSWORD", "test_sentinel_pw"
        )
    router = importlib.import_module("agent_mcp.router.app")
    # PR-C extracted the lifecycle state machine into
    # ``project_orchestrator``; ``router/app.py`` re-exports
    # ``_systemctl`` so legacy tests that monkeypatch it via the
    # router module keep working, but the actual function call sites
    # live in the orchestrator module, so we patch both attribute
    # bindings to point at the same stub.
    from agent_mcp.router import project_orchestrator as _po
    monkeypatch.setattr(router, "_systemctl", systemctl_stub)
    monkeypatch.setattr(_po, "_systemctl", systemctl_stub)
    # The reaper/reconcile hooks call _systemctl too; stub already
    # handles that, but the reaper itself never runs in unit tests
    # because we never wire it up via web.run_app — make_app() only
    # registers it on .on_startup, which the aiohttp TestServer DOES
    # invoke. Patch the start hook to a no-op so we don't have a
    # rogue reaper task running across tests.
    async def _noop(app):  # noqa: D401 - tiny stub
        return None

    monkeypatch.setattr(router, "_start_reaper_task", _noop)
    monkeypatch.setattr(router, "_start_alias_reaper_task", _noop)
    monkeypatch.setattr(router, "reconcile_on_startup", _noop)
    return router


@pytest.fixture
def router_app(router_module):
    """The aiohttp ``web.Application`` returned by ``make_app()``.

    Tests typically use this via the ``aiohttp_client`` fixture from
    pytest-aiohttp::

        async def test_x(aiohttp_client, router_app):
            client = await aiohttp_client(router_app)
            resp = await client.get("/agent-mcp/api/router/projects")
            ...
    """
    return router_module.make_app()


_SENTINEL_USERNAME = "test_sentinel_op"
_SENTINEL_PASSWORD = "test_sentinel_pw"


import pytest_asyncio  # noqa: E402 — kept local to the fixture


@pytest_asyncio.fixture
async def aiohttp_client(aiohttp_client_cls, request, router_env):
    """Override pytest-aiohttp's ``aiohttp_client`` to auto-login.

    PR D (prancy-napping-pie) added a router-side middleware that
    enforces an operator session on every dashboard mutation/read.
    Legacy tests don't care about identity — they care about the
    proxy, registry, dashboard, etc. — so we automatically log in
    the sentinel operator seeded by ``router_module`` and attach
    the resulting session cookie to the TestClient's jar.

    Tests that need the unauthenticated state (the new PR D
    auth-gate tests, plus the setup-wizard tests that rely on the
    empty-users middleware bouncing them) opt out with
    ``@pytest.mark.no_auth_seed_session`` (or implicitly via
    ``@pytest.mark.no_seed_operator``, which skips the operator
    seed entirely).

    Implementation notes:

    * The factory wrapper around pytest-aiohttp's own ``go``
      (re-implemented here because the upstream fixture's ``go`` is
      not exposed as a public API). On exit we close every TestClient
      we made.
    * The login round-trip uses ``client.post("/agent-mcp/login", ...)``
      which goes through the SAME middleware stack we're about to test
      against — and the login route is allow-listed by the middleware,
      so the post never 401s. On a successful login the TestClient's
      cookie_jar absorbs ``Set-Cookie`` and replays it on subsequent
      requests automatically.
    """
    from aiohttp.test_utils import TestServer

    skip_login = (
        request.node.get_closest_marker("no_auth_seed_session") is not None
        or request.node.get_closest_marker("no_seed_operator") is not None
    )

    clients: list = []
    servers: list = []

    async def go(app, **kwargs):
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        servers.append(server)
        client = aiohttp_client_cls(server, **kwargs)
        await client.start_server()
        clients.append(client)
        if not skip_login:
            # The sentinel operator is seeded by ``router_module``
            # via the env-var bootstrap. Convert that into a live
            # session by hitting /login; the cookie jar persists.
            resp = await client.post(
                "/agent-mcp/login",
                data={
                    "username": _SENTINEL_USERNAME,
                    "password": _SENTINEL_PASSWORD,
                },
                allow_redirects=False,
            )
            # On a 303 the cookie is set; on anything else, leave
            # the client as-is so the test can introspect the failure.
            assert resp.status in (303, 401), (
                f"unexpected sentinel login status {resp.status}"
            )
        return client

    yield go

    for c in clients:
        await c.close()
    for s in servers:
        await s.close()


@pytest.fixture
def write_dashboard_file(router_env: _RouterEnv) -> Callable[[str, str], Path]:
    """Helper to drop an HTML/JS/etc file into the dashboard tree."""

    def _write(relpath: str, content: str) -> Path:
        target = router_env.dashboard_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    return _write


@pytest.fixture
def register_project(
    router_module, router_env: _RouterEnv, request,
) -> Callable[[str, str | None], Path]:
    """Register a project in the test registry, returning its workspace.

    Phase 1 PR D: after registering, also grant the sentinel
    operator membership in the project so the
    ``require_operator_session_middleware`` passes the membership
    check for tests that hit ``/agent-mcp/api/<name>/...`` or
    ``/agent-mcp/app/<name>/...`` with the auto-attached sentinel
    cookie.

    Tests marked ``@pytest.mark.no_seed_operator`` skip the
    sentinel-operator bootstrap entirely; for those, the membership
    grant is a no-op (no operator exists to grant to).

    Tests marked ``@pytest.mark.no_auth_seed_session`` still want
    the sentinel operator to exist (so the empty-users middleware
    doesn't bounce them) but want to control the cookie themselves;
    they may seed their own users + memberships per scenario.
    """
    skip_seed_op = (
        request.node.get_closest_marker("no_seed_operator") is not None
    )

    def _register(name: str, workspace: str | None = None) -> Path:
        ws = Path(workspace) if workspace else (router_env.root / "ws" / name)
        ws.mkdir(parents=True, exist_ok=True)
        router_module._REGISTRY.register(name, str(ws))
        if not skip_seed_op:
            # router.db migrations run on app startup; for tests that
            # haven't built the app yet, run them eagerly so the
            # users table exists.
            from agent_mcp.router import identity as _identity
            try:
                _identity.run_router_migrations_upgrade()
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                row = _identity.get_user_by_username(_SENTINEL_USERNAME)
            except Exception:
                row = None
            if row is None:
                # The on_startup bootstrap hasn't fired yet (e.g.,
                # single-tenant test variants build the app
                # differently); create the sentinel here.
                try:
                    _identity.create_user(
                        username=_SENTINEL_USERNAME,
                        password=_SENTINEL_PASSWORD,
                    )
                    row = _identity.get_user_by_username(_SENTINEL_USERNAME)
                except Exception:  # pragma: no cover - defensive
                    row = None
            if row is not None:
                _identity.add_project_membership(row["user_id"], name)
        return ws

    return _register


# ── Disable the project-wide _isolate_env autouse fixture for this
#    subdirectory. The router tests don't touch agent_mcp.core.globals
#    or OPENAI_API_KEY and the env-mucking only causes confusion.
#    (The fixture lives in tests/conftest.py; this `autouse=False`
#    override at deeper scope would not actually disable it — we just
#    let it run, since the env vars it clobbers aren't ones the router
#    cares about.) — kept as a note rather than a fixture so we don't
#    accidentally override real isolation.
