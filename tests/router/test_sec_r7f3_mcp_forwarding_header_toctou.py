"""R7-F3 (HIGH, live-exploited) — stale-forwarding-header TOCTOU on
``POST /agent-mcp/mcp/<project>`` (the cookie-forwarding MCP transport).

Background
----------
``backend_mcp_handler`` resolves the calling operator's LIVE per-project
role via ``_forwarding_header_from_cookie`` (a DB read) and signs a
30-second-TTL HMAC ``X-Agent-MCP-Forwarded-Operator`` header carrying
that role — this used to happen BEFORE ``_proxy_to_backend`` read the
request body (``req_body = await req.read()``, a genuine client-paced
yield point). Only after the full body arrived did the router forward
the request, with the ALREADY-SIGNED, now possibly-stale header, to the
backend. The backend's ``AuthHeaderMiddleware`` trusts the embedded role
verbatim for the header's full TTL — no live re-check on that side.

Live-exploited repro (pentest-loop R7-F3): operator ``dev`` (operator on
project ``verify-scaffold``) opens a slow-drip ``POST /agent-mcp/mcp/
verify-scaffold`` (a ``tools/call`` → ``terminate_agent`` body), holding
most of the body back. Mid-pause, a sysadmin demotes ``dev``'s project
role ``operator`` → ``viewer`` (committed). The paused request then
resumes and completes its body read — pre-fix this still executes
using dev's PRE-demotion ``operator`` role; post-fix it must be denied.

Fix
---
``_proxy_to_backend`` now takes ``inject_header_resolver`` (an async
closure) instead of a precomputed ``inject_header`` tuple. The cookie
path in ``backend_mcp_handler`` still calls
``_forwarding_header_from_cookie`` once at entry (a cheap early-401
gate — unauthenticated / non-member callers never reach the backend
at all), but the header actually ATTACHED to the upstream request is
resolved AGAIN, from a fresh DB read, by the resolver — invoked inside
``_proxy_to_backend`` immediately after ``req_body = await req.read()``
succeeds. A demotion committed while the body was in flight is
therefore picked up before the header is signed, not after.

These tests reproduce the race DETERMINISTICALLY (no real sleeps): a
monkeypatched ``aiohttp.web.Request.read`` pauses the ATTACK request on
an ``asyncio.Event`` right when ``_proxy_to_backend`` reads the body;
the demotion PATCH commits while it's paused; the read is then
released and the paused request resumes.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.no_auth_seed_session,
]


_REST_HEADERS = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Short on purpose: this becomes a UNIX domain socket path component
# (``<sock_dir>/<name>/backend.sock``) and AF_UNIX caps sun_path at
# 108 bytes. Under pytest-xdist, tmp_path already carries a
# ``popen-gwN`` worker prefix — a long project name here pushed the
# full socket path over that limit and made the fixture's ``bind()``
# fail intermittently only under ``-n auto``.
_PROJECT = "r7f3"


# ── Helpers (mirror test_forwarding_header_signing.py / R6-F2 test) ──


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(username: str, password: str = "pw") -> str:
    return _identity_module().create_user(username=username, password=password)


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


# ── Fake backend: verifies the forwarding header AND gates a tool ───


class _RoleGatedBackend:
    """UDS-bound aiohttp app that verifies the forwarding-header HMAC
    and gates a simulated ``terminate_agent`` tool call on the
    forwarded role — ``operator`` is admitted (mirrors the real
    ``agents.terminate`` capability, which is operator-tier), any
    other verified role (or no header at all) is denied.

    Every request is recorded (including the role the router actually
    forwarded) so the test can assert on it directly, independent of
    the HTTP status code.
    """

    def __init__(self, hmac_key: bytes) -> None:
        self.hmac_key = hmac_key
        self.records: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._gated)
        return app

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
        rec = {
            "method": req.method,
            "path": req.path,
            "body": body,
            "operator_id": operator_id,
            "role": role,
        }
        self.records.append(rec)
        if operator_id is None:
            return web.json_response(
                {"error": "forwarding header required"}, status=401,
            )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        tool_name = (payload.get("params") or {}).get("name")
        if tool_name == "terminate_agent" and role != "operator":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": -32000,
                        "message": (
                            f"Unauthorized: {role!r} lacks capability "
                            f"agents.terminate"
                        ),
                    },
                },
                status=403,
            )
        return web.json_response(
            {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}},
        )


async def _start_backend_on_uds(
    backend: _RoleGatedBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def race_backend(router_module, router_env, systemctl_stub):
    """Stand up the role-gated fake backend for project ``_PROJECT``,
    pre-populating its HMAC key on disk the way the systemd unit's
    ExecStartPre would in production (mirrors ``wave2_backend``)."""
    from agent_mcp.router import project_orchestrator as _po

    name = _PROJECT
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"

    hmac_key = secrets.token_bytes(32)
    key_path = _po._forwarding_hmac_path(name)
    key_path.write_bytes(hmac_key)
    os.chmod(key_path, 0o600)
    assert _po.ensure_forwarding_hmac_key(name) == hmac_key

    backend = _RoleGatedBackend(hmac_key=hmac_key)
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()
        _po.forwarding_hmac_keys.pop(name, None)


def _terminate_agent_payload() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "terminate_agent",
                "arguments": {"agent_id": "worker-1"},
            },
        }
    ).encode()


async def _demote_to_viewer(admin_client, user_id: str, cookie: str):
    return await admin_client.patch(
        f"/agent-mcp/api/router/projects/{_PROJECT}/memberships/u:{user_id}",
        data=json.dumps({"role": "viewer"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


# ── Test A: the LIVE-EXPLOITED repro ─────────────────────────────────


async def test_slow_drip_terminate_agent_rejects_stale_operator_role(
    aiohttp_client, router_app, race_backend, monkeypatch,
) -> None:
    """A slow-drip ``terminate_agent`` call must be gated on the LIVE
    role at the moment the header is attached, not the role resolved
    before the client's body-read yield point.

    ``dev`` (operator on the project) opens the call; while dev's
    body-read is paused, a sysadmin demotes dev to viewer — the
    demotion COMMITS before dev's request resumes. Pre-fix this still
    forwards ``role=operator`` (the stale, pre-demotion snapshot) and
    the fake backend admits it (200); post-fix the resolver re-reads
    the DB post-pause, forwards ``role=viewer``, and the fake backend
    denies it (403).
    """
    identity = _identity_module()

    # Stand the app up first so the sentinel (env-var bootstrap) is the
    # first user / sysadmin; dev and root2 are then ordinary users.
    admin_client = await aiohttp_client(router_app)
    sysadmin_cookie = await _login(
        admin_client, "test_sentinel_op", "test_sentinel_pw",
    )

    dev_id = _seed_user("dev")
    identity.insert_project_membership(_PROJECT, user_id=dev_id, role="operator")

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev")

    # Sanity: dev really is 'operator' before the race starts.
    from agent_mcp.router import group_resolver

    assert (
        group_resolver.resolve_user_project_role(dev_id, _PROJECT) == "operator"
    )

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    paused = {"done": False}
    original_read = web.Request.read
    target_path = f"/agent-mcp/mcp/{_PROJECT}"

    async def paused_read(self):
        if (
            not paused["done"]
            and self.method == "POST"
            and self.path == target_path
        ):
            paused["done"] = True
            body_read_started.set()
            await release_body_read.wait()
        return await original_read(self)

    monkeypatch.setattr(web.Request, "read", paused_read)

    async def _attack():
        return await client.post(
            target_path,
            data=_terminate_agent_payload(),
            headers=_MCP_HEADERS,
            cookies={"agent_mcp_session": dev_cookie},
            allow_redirects=False,
        )

    attack_task = asyncio.ensure_future(_attack())
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    demote_resp = await _demote_to_viewer(admin_client, dev_id, sysadmin_cookie)
    assert demote_resp.status == 200, await demote_resp.text()
    assert (
        group_resolver.resolve_user_project_role(dev_id, _PROJECT) == "viewer"
    ), "demotion must be committed before the paused request resumes"

    release_body_read.set()
    resp = await asyncio.wait_for(attack_task, timeout=5)

    posts = [r for r in race_backend.records if r["path"] == "/mcp"]
    assert len(posts) == 1, (
        f"expected exactly one /mcp record; got "
        f"{[(r['method'], r['path']) for r in race_backend.records]!r}"
    )
    assert posts[0]["role"] == "viewer", (
        f"the router forwarded role={posts[0]['role']!r} to the backend; "
        f"expected the LIVE post-demotion role 'viewer' — forwarding "
        f"'operator' means the stale pre-demotion snapshot survived the "
        f"race (R7-F3 regression)"
    )
    body = await resp.json()
    assert resp.status == 403, (
        f"terminate_agent must be denied post-demotion; got {resp.status}: "
        f"{body!r}"
    )
    assert "unauthorized" in json.dumps(body).lower()


# ── Test B: control — a demotion with NO in-flight race still denies ─


async def test_non_racing_demotion_still_denies_terminate_agent(
    aiohttp_client, router_app, race_backend,
) -> None:
    """Control (mandatory sanity check): a ``terminate_agent`` call
    issued AFTER an uncontested demotion, with NO paused in-flight
    request, must correctly reject. Proves the capability gate itself
    works and any bypass under test is purely the staleness window,
    not a broken gate."""
    identity = _identity_module()

    admin_client = await aiohttp_client(router_app)
    sysadmin_cookie = await _login(
        admin_client, "test_sentinel_op", "test_sentinel_pw",
    )

    dev_id = _seed_user("dev2")
    identity.insert_project_membership(_PROJECT, user_id=dev_id, role="operator")

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev2")

    demote_resp = await _demote_to_viewer(admin_client, dev_id, sysadmin_cookie)
    assert demote_resp.status == 200, await demote_resp.text()

    resp = await client.post(
        f"/agent-mcp/mcp/{_PROJECT}",
        data=_terminate_agent_payload(),
        headers=_MCP_HEADERS,
        cookies={"agent_mcp_session": dev_cookie},
        allow_redirects=False,
    )
    body = await resp.json()
    assert resp.status == 403, (
        f"a non-racing post-demotion call must be denied; got "
        f"{resp.status}: {body!r}"
    )
    posts = [r for r in race_backend.records if r["path"] == "/mcp"]
    assert posts[-1]["role"] == "viewer"


# ── Test C: regression — a non-racing operator call still succeeds ──


async def test_non_racing_operator_terminate_agent_still_succeeds(
    aiohttp_client, router_app, race_backend,
) -> None:
    """Regression: an uncontested operator call (no demotion at all)
    must still succeed — the fix must not spuriously deny the common
    path."""
    identity = _identity_module()

    await aiohttp_client(router_app)  # boots the sentinel/first-user

    dev_id = _seed_user("dev3")
    identity.insert_project_membership(_PROJECT, user_id=dev_id, role="operator")

    client = await aiohttp_client(router_app)
    dev_cookie = await _login(client, "dev3")

    resp = await client.post(
        f"/agent-mcp/mcp/{_PROJECT}",
        data=_terminate_agent_payload(),
        headers=_MCP_HEADERS,
        cookies={"agent_mcp_session": dev_cookie},
        allow_redirects=False,
    )
    body = await resp.json()
    assert resp.status == 200, (
        f"a genuine, uncontested operator call must succeed; got "
        f"{resp.status}: {body!r}"
    )
    posts = [r for r in race_backend.records if r["path"] == "/mcp"]
    assert posts[-1]["role"] == "operator"
