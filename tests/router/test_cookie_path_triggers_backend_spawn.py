"""F015 v5 — cookie path must trigger backend spawn before reading HMAC.

Bug history
-----------

F015 v4 (PR #214) correctly inverted ownership of the per-project
HMAC key: the systemd unit's ExecStartPre generates the file, the
router only reads. The router-side
``ensure_forwarding_hmac_key`` was demoted to a pure reader.

But ``_forwarding_header_from_cookie`` (router/app.py) was not
updated to match. It still reads the key file BEFORE anything
triggers a ``systemctl start``. On a cold backend (router just
booted, no agent-side bearer traffic yet, dashboard is the first
caller), nothing has invoked ``_ensure`` for the project, so the
systemd unit hasn't started, so ExecStartPre hasn't run, so the
key file doesn't exist. The reader returns ``None``, the cookie
handler returns ``None``, the caller maps it to 401, the dashboard
retries — and each retry hits the same dead-cache path because
nothing in the cookie code path EVER calls ``_ensure``.

Live VM symptom (pre-fix):

  * ``agent-mcp@demo-proj.service`` status: ``inactive (dead)``
  * ``/run/agent-mcp/demo-proj/forwarding_hmac``: does not exist
  * Dashboard cookie request: 401 in a tight loop

What this test pins
-------------------

A cookie-authenticated request against a backend whose systemd
unit has NEVER been started must:

  1. Trigger the unit start (via ``_ensure``) from inside
     ``_forwarding_header_from_cookie``.
  2. The unit's ExecStartPre (simulated in this test by a
     systemctl-stub side effect) writes the HMAC file.
  3. The cookie path reads the file, signs the header, the request
     reaches the backend and verifies → 200.

Test-fixture pitfall the F015 v4 tests fell into
------------------------------------------------

The pre-existing ``wave2_backend`` fixture in
``test_forwarding_header_signing.py`` pre-writes the HMAC file
in the fixture body, then marks the unit ``active`` in the stub.
That bypasses the bug entirely: the cookie path's read succeeds
because someone else (the fixture) wrote the file. The bug only
surfaces when nothing has triggered ``_ensure`` yet.

The fixture below deliberately does NOT pre-write the file, does
NOT pre-add the unit to ``active_units``, and uses a systemctl
stub that simulates ExecStartPre — i.e. writes the file when
``systemctl start <unit>`` is invoked. The "is the file present
after the cookie request" assertion is the load-bearing one.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


# ── Helpers (mirrored from test_forwarding_header_signing.py) ──────


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


# ── ExecStartPre-aware systemctl stub ──────────────────────────────


@dataclass
class _ExecStartPreSystemctl:
    """Like ``_SystemctlRecorder`` but with an ExecStartPre callback.

    Production systemd contract (F015 v4, ``nix/module.nix``): when
    the unit transitions from inactive → active via ``systemctl
    start``, the unit's ``ExecStartPre`` runs FIRST and generates
    the HMAC key file. ``ExecStart`` (the backend launcher) runs
    after, finds the key already on disk, mmaps it for verifying
    forwarding headers.

    The base ``_SystemctlRecorder`` only flips an in-memory bool;
    it doesn't model the on-disk side effect. This subclass adds
    that side effect so the test exercises the real production
    code path: the cookie handler triggers ``systemctl start``,
    the (simulated) ExecStartPre writes the file, the next reader
    call finds it on disk.

    The callback is parameterised so the test can also bind a UDS
    socket at unit start (the backend's launcher would normally
    create the socket; the test fakes that by binding a fake
    aiohttp backend after the start verb fires).
    """

    on_start_or_restart: object = None  # callable: (unit:str) -> None
    calls: list[tuple[str, ...]] = field(default_factory=list)
    active_units: set[str] = field(default_factory=set)
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
            if verb in ("start", "restart"):
                # ExecStartPre side effect happens BEFORE we flip the
                # unit to active, mirroring systemd's ordering.
                if self.on_start_or_restart is not None:
                    self.on_start_or_restart(unit)
                self.active_units.add(unit)
            elif verb == "stop":
                self.active_units.discard(unit)
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout="", stderr="",
        )


# ── Fake backend that verifies the forwarding header ────────────────


class _VerifyingBackend:
    """UDS-bound aiohttp app that verifies the forwarding-header HMAC.

    Mirrors the production ``AuthHeaderMiddleware`` verify path —
    401 on missing/invalid header, 200 on a header that verifies
    against the per-project key.

    Unlike the other tests' ``_Wave2Backend``, the key it verifies
    against is READ FROM DISK lazily on each request — the test
    must NOT capture the key bytes at fixture-build time, because
    the key doesn't exist yet (ExecStartPre hasn't been simulated
    when the fixture builds the backend).
    """

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._gated)
        return app

    async def _gated(self, req: web.Request) -> web.Response:
        from agent_mcp.app import forwarding_header as _fh

        raw = req.headers.get(_fh.HEADER_NAME)
        operator_id = None
        if raw is not None:
            try:
                key = self.key_path.read_bytes()
            except FileNotFoundError:
                key = None
            if key:
                operator_id = _fh.verify(raw, key)
        body = await req.read()
        self.records.append(
            {
                "method": req.method,
                "path": req.path,
                "headers": dict(req.headers),
                "body": body,
                "operator_id": operator_id,
            },
        )
        if operator_id is None:
            return web.Response(status=401, body=b"forwarding header required")
        return web.Response(
            body=b'{"ok":true}', content_type="application/json",
        )


async def _start_backend_on_uds(
    backend: _VerifyingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


# ── Fixture: cold backend, ExecStartPre simulated by stub ───────────


@pytest_asyncio.fixture
async def cold_backend(router_env, monkeypatch):
    """Register a project, install an ExecStartPre-aware systemctl
    stub, but do NOT pre-write the HMAC file and do NOT mark the unit
    active. The unit transitions to active only when something
    invokes ``systemctl start`` — which is exactly the production
    cold-start state.

    Re-imports the router with the ExecStartPre stub bound, since
    the default ``systemctl_stub`` fixture doesn't model file
    side-effects.
    """
    # Drop router modules so the next import re-runs module-level
    # env reads against this fixture's env (mirrors what
    # ``router_module`` does, but we need to install OUR stub
    # before make_app() runs).
    for mod_name in (
        "agent_mcp.router",
        "agent_mcp.router.app",
        "agent_mcp.router.project_orchestrator",
        "agent_mcp.router.project_registry",
        "agent_mcp.router.identity",
        "agent_mcp.router.login",
        "agent_mcp.router.setup_wizard",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.sso",
    ):
        sys.modules.pop(mod_name, None)
    # Bootstrap a sentinel operator so the empty-users middleware
    # no-ops; the test creates its own non-sysadmin user for the
    # cookie request.
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "test_sentinel_op")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_PASSWORD", "test_sentinel_pw")

    import importlib

    router = importlib.import_module("agent_mcp.router.app")
    from agent_mcp.router import project_orchestrator as _po

    name = "cold-demo"
    router._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    key_path = _po._forwarding_hmac_path(name)

    # Make sure the test starts clean — neither file exists.
    key_path.unlink(missing_ok=True)
    sock.unlink(missing_ok=True)
    _po.forwarding_hmac_keys.pop(name, None)

    # ExecStartPre simulator: when systemctl starts the unit, write
    # the HMAC file just like ``nix/module.nix``'s ExecStartPre
    # would. Also bind the fake backend on the UDS (the launcher
    # script would normally create the socket; we fake it here so
    # ``_ensure``'s socket-poll succeeds).
    backend = _VerifyingBackend(key_path=key_path)
    runner_box: dict[str, web.AppRunner] = {}

    def _exec_start_pre(unit: str) -> None:
        # 1. ExecStartPre: write the HMAC key if not present.
        if not key_path.exists():
            key_bytes = secrets.token_bytes(32)
            key_path.write_bytes(key_bytes)
            os.chmod(key_path, 0o600)
        # 2. The backend launcher itself (ExecStart) — bind the UDS.
        # Done synchronously via a stash that the fixture body picks
        # up after the systemctl call returns. We can't await here
        # (the systemctl stub is sync), so we set a sentinel and
        # bind in a tiny wrapper in the test if needed. In practice
        # the simplest path is to pre-bind the UDS BEFORE the
        # cookie request fires — the bug isn't about socket bind
        # ordering, it's about the HMAC file. So we bind the UDS
        # synchronously here using a sync placeholder.
        # ↓ Implementation: bind via runner_box during the test.

    stub = _ExecStartPreSystemctl(on_start_or_restart=_exec_start_pre)
    monkeypatch.setattr(router, "_systemctl", stub)
    monkeypatch.setattr(_po, "_systemctl", stub)

    async def _noop(app):
        return None

    monkeypatch.setattr(router, "_start_reaper_task", _noop)
    monkeypatch.setattr(router, "_start_alias_reaper_task", _noop)
    monkeypatch.setattr(router, "reconcile_on_startup", _noop)

    # Pre-bind the UDS so ``_ensure``'s socket-existence poll passes.
    # The bug under test is about the HMAC file, not socket creation;
    # binding the socket up front isolates the failure mode. The
    # ExecStartPre stub above leaves socket creation to this binding.
    runner = await _start_backend_on_uds(backend, sock)
    runner_box["runner"] = runner

    try:
        yield {
            "router": router,
            "name": name,
            "sock": sock,
            "key_path": key_path,
            "backend": backend,
            "stub": stub,
        }
    finally:
        await runner.cleanup()
        _po.forwarding_hmac_keys.pop(name, None)


# ── The test ────────────────────────────────────────────────────────


async def test_cookie_request_on_cold_backend_triggers_spawn_and_succeeds(
    aiohttp_client, cold_backend,
) -> None:
    """A cookie request against a never-spawned backend must
    succeed (200) and end with the HMAC key file on disk.

    Pre-fix this test fails: ``_forwarding_header_from_cookie`` reads
    the key file before anything has invoked ``systemctl start``, so
    the key file doesn't exist, the reader returns ``None``, the
    handler returns 401, and ``systemctl start`` is NEVER called for
    the unit (so the simulated ExecStartPre never fires either).

    Post-fix the handler explicitly invokes ``_ensure`` before
    reading; the stub's start callback (modelling ExecStartPre) then
    writes the file; the reader finds it; the cookie path signs the
    forwarding header; the backend verifies; 200.
    """
    router = cold_backend["router"]
    name = cold_backend["name"]
    key_path = cold_backend["key_path"]
    backend = cold_backend["backend"]
    stub = cold_backend["stub"]
    unit = f"agent-mcp@{name}.service"

    # Pre-conditions — the bug we're fixing depends on these.
    assert not key_path.exists(), (
        "Test bug: HMAC key file must NOT exist before the cookie "
        "request — that's the very condition the cookie path is "
        "supposed to trigger ExecStartPre for. Pre-writing it would "
        "bypass the bug entirely (the F015 v4 fixture mistake)."
    )
    assert unit not in stub.active_units, (
        "Test bug: unit must NOT be pre-marked active. The cookie "
        "path must be what triggers the start."
    )

    client = await aiohttp_client(router.make_app())
    # Sentinel was seeded by the env-var bootstrap — alice becomes
    # the second user (no auto-sysadmin grant) so the membership
    # check exercises the real path.
    alice_id = _seed_user("alice")
    _identity_module().add_project_membership(alice_id, name)
    alice_cookie = await _login(client, "alice")

    # Sanity: cache is empty too.
    from agent_mcp.router import project_orchestrator as _po
    assert _po.forwarding_hmac_keys.get(name) is None

    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    resp = await client.post(
        f"/agent-mcp/mcp/{name}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        cookies={"agent_mcp_session": alice_cookie},
        allow_redirects=False,
    )

    body = await resp.text()
    assert resp.status == 200, (
        f"cookie request on a cold backend should succeed once the "
        f"handler triggers ExecStartPre; got {resp.status}: {body!r}. "
        f"systemctl calls: {stub.calls!r}"
    )

    # Post-conditions — these prove the production code path ran.
    assert key_path.exists(), (
        "HMAC key file is still missing after the cookie request. "
        "The fix should have invoked ``_ensure`` which calls "
        "``systemctl start``, which fires the (stubbed) ExecStartPre, "
        "which writes the file."
    )
    assert unit in stub.active_units, (
        f"unit was never started — cookie path bailed out before "
        f"reaching ``_ensure``. systemctl calls: {stub.calls!r}"
    )
    assert stub.counts[("start", unit)] >= 1, (
        f"expected at least one ``systemctl start {unit}``; got counts "
        f"{dict(stub.counts)!r}"
    )

    # And the proxied request reached the backend with a verified header.
    posts = [r for r in backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"expected one /mcp record; got "
        f"{[(r['method'], r['path']) for r in backend.records]!r}"
    )
    assert posts[0]["operator_id"] == alice_id
