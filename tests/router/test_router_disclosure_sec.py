"""Security hardening — information-disclosure + project-existence oracle.

Two owner-authorised defensive fixes on the router surface (deferred
from the SEC1 sweep because they live in ``router/app.py``):

ITEM 1 (LOW) — version + absolute-path disclosure
  * The ``GET /agent-mcp/`` service descriptor and the public
    ``GET /api/router/health`` probe used to echo the internal package
    version. Operators don't consume it (the dashboard hard-codes its
    own product string), so it's pure attacker-useful fingerprinting.
  * The cross-project overview envelope echoed each project's ABSOLUTE
    workspace path (e.g. ``/var/lib/agent-mcp/projects/<proj>`` or
    ``/home/<user>/.local/share/agent-mcp/projects/<proj>``), disclosing
    the server's filesystem layout + the deploy's home directory. The
    envelope now carries a project-relative label instead.

ITEM 2 (LOW) — pre-auth project-existence oracle
  * The MCP handler resolved the project (404 for unknown) BEFORE the
    auth check (401 for known-but-unauthenticated). An anonymous caller
    could therefore enumerate valid project names by status-code
    differencing (unknown→404 vs known→401). The credential-PRESENCE
    gate closed the fully-anonymous probe, but a junk
    ``Authorization: Bearer <garbage>`` cleared that gate and still hit
    the resolve → unknown→404 vs known→401 remained observable. The
    handler now ALSO collapses the resolve's 404 into the same uniform
    401 for any not-yet-authenticated caller (the router can't validate
    a bearer itself), so unknown and known are indistinguishable on the
    MCP transport regardless of what junk credential is presented.
    Genuine 404 semantics for unknown projects survive on the
    operator-session-gated REST/lifecycle handlers, which an
    unauthenticated caller can't reach.

ITEM 3 (LOW) — reason-phrase reflection
  * Several router/admin handlers reflected an attacker-controlled
    project name / alias / agent_id into the HTTP ``reason`` (status
    line): ``reason=f"unknown project: {name!r}"`` etc. Echoing caller
    input into the status line is response-splitting-adjacent and
    leaks nothing an operator needs; every such site now emits a fixed
    constant phrase.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}

# Any absolute POSIX path that looks like a server filesystem location.
# The overview envelope + descriptors must never carry one of these.
_ABS_PATH_RE = re.compile(r"(/var/lib/|/home/|/root/|/tmp/|/run/)")


# ── ITEM 1: version disclosure ──────────────────────────────────────


async def test_service_descriptor_omits_version(
    aiohttp_client, router_app, register_project,
) -> None:
    """``GET /agent-mcp/`` (JSON descriptor) must NOT leak the internal
    package version — it's operator-useless fingerprinting."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/", allow_redirects=False)

    assert resp.status == 200
    body = await resp.json()
    assert "version" not in body, (
        f"service descriptor still leaks version: {body.get('version')!r}"
    )


@pytest.mark.no_auth_seed_session
async def test_health_probe_omits_version(
    aiohttp_client, router_app,
) -> None:
    """The public ``GET /api/router/health`` liveness probe must NOT
    leak the internal package version either."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/health", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    assert body.get("ok") is True
    assert "version" not in body, (
        f"health probe still leaks version: {body.get('version')!r}"
    )


# ── ITEM 1: absolute workspace-path disclosure ──────────────────────


async def test_overview_workspace_is_not_absolute_path(
    aiohttp_client, router_app, register_project,
) -> None:
    """The overview envelope must expose a project-RELATIVE workspace
    label, never the server's absolute filesystem path."""
    register_project("alpha")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/overview", headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200
    body = await resp.json()
    row = next(r for r in body["projects"] if r["name"] == "alpha")
    ws = row["workspace"]
    assert not ws.startswith("/"), (
        f"overview workspace is an absolute path: {ws!r}"
    )
    assert not _ABS_PATH_RE.search(ws), (
        f"overview workspace discloses a server filesystem location: {ws!r}"
    )
    # The relative label still identifies the project's directory.
    assert ws == "alpha"


async def test_overview_envelope_carries_no_absolute_paths_anywhere(
    aiohttp_client, router_app, register_project,
) -> None:
    """Defence in depth: no field anywhere in the overview envelope may
    carry an absolute ``/var/lib`` / ``/home`` / … path."""
    register_project("alpha")
    register_project("beta")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/overview", headers=_STRICT_ACCEPT,
    )
    text = await resp.text()
    assert not _ABS_PATH_RE.search(text), (
        f"overview envelope leaks an absolute server path: {text!r}"
    )


# ── ITEM 1: create-project SUCCESS envelope (SD-R34-1) ──────────────


async def test_create_project_success_workspace_is_relative_label(
    aiohttp_client, router_app,
) -> None:
    """SD-R34-1: the create-project SUCCESS envelope must expose the SAME
    project-RELATIVE workspace label the overview handler uses — never the
    fully-resolved ABSOLUTE server path (which discloses the deployment's
    filesystem layout / the service account's home dir to any
    ``system.projects.manage`` holder, including a delegated-cap
    non-sysadmin).

    Missed sibling: overview already scrubs via ``_workspace_label`` and
    the SD-R15 series scrubbed the create/rename/delete ERROR paths; the
    create SUCCESS path was the last leaker.
    """
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data='{"name": "alpha"}',
        headers={
            "Accept": "application/vnd.agent-mcp.v1+json",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 201, await resp.text()
    body = await resp.json()
    ws = body["project"]["workspace"]
    assert not ws.startswith("/"), (
        f"create workspace is an absolute path: {ws!r}"
    )
    assert not _ABS_PATH_RE.search(ws), (
        f"create workspace discloses a server filesystem location: {ws!r}"
    )
    # Same relative label the overview handler emits — just the name for
    # the common ``<default-parent>/<name>`` layout.
    assert ws == "alpha"


async def test_create_project_register_error_scrubs_absolute_path(
    aiohttp_client, router_app, router_module, monkeypatch,
) -> None:
    """SD-R34-1 class-sweep: the create ERROR path (``_REGISTRY.register``
    raising a ValueError) must not reflect the registry's absolute-path
    text (``… already registered at '<abs>'; refusing to re-point at
    '<abs>'``) into the client envelope. Force the ValueError and assert
    the response body carries the generic ``already_registered`` message
    with NO server filesystem path.
    """

    def _boom(name, workspace, **extra):
        raise ValueError(
            f"project {name!r} is already registered at "
            f"'/var/lib/agent-mcp/projects/{name}'; refusing to re-point "
            f"at {workspace!r}"
        )

    monkeypatch.setattr(router_module._REGISTRY, "register", _boom)
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data='{"name": "collide"}',
        headers={
            "Accept": "application/vnd.agent-mcp.v1+json",
            "Content-Type": "application/json",
        },
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "already_registered"
    text = await resp.text()
    assert not _ABS_PATH_RE.search(text), (
        f"create register-error envelope leaks an absolute path: {text!r}"
    )


# ── ITEM 2: pre-auth project-existence oracle ───────────────────────


@pytest.mark.no_auth_seed_session
async def test_anonymous_mcp_post_uniform_401_known_vs_unknown(
    aiohttp_client, router_app, register_project,
) -> None:
    """An UNAUTHENTICATED POST to the MCP transport must return the SAME
    status (401) whether the project is known or unknown — otherwise an
    attacker enumerates valid project names by status-code differencing
    (unknown→404 vs known→401).

    No bearer, no cookie (``no_auth_seed_session`` skips the sentinel
    login), so both requests are genuinely anonymous.
    """
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    hdrs = {"Content-Type": "application/json"}

    resp_known = await client.post(
        "/agent-mcp/mcp/known-proj", data=body, headers=hdrs,
        allow_redirects=False,
    )
    resp_unknown = await client.post(
        "/agent-mcp/mcp/does-not-exist", data=body, headers=hdrs,
        allow_redirects=False,
    )

    assert resp_known.status == 401, await resp_known.text()
    assert resp_unknown.status == 401, await resp_unknown.text()
    assert resp_known.status == resp_unknown.status, (
        "project-existence oracle: anonymous POST to a known project "
        f"returned {resp_known.status} but an unknown project returned "
        f"{resp_unknown.status}"
    )


@pytest.mark.no_auth_seed_session
async def test_anonymous_mcp_401_carries_www_authenticate(
    aiohttp_client, router_app, register_project,
) -> None:
    """The uniform 401 keeps the ``WWW-Authenticate`` header so a real
    MCP client still learns which realm to authenticate against — the
    hardening must not degrade the legitimate auth-challenge UX."""
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    for project in ("known-proj", "does-not-exist"):
        resp = await client.post(
            f"/agent-mcp/mcp/{project}",
            data=body,
            headers={"Content-Type": "application/json"},
            allow_redirects=False,
        )
        assert resp.status == 401
        assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.no_auth_seed_session
async def test_junk_bearer_mcp_unknown_project_is_401_not_404(
    aiohttp_client, router_app, register_project,
) -> None:
    """SEC5 core: a junk ``Authorization: Bearer <garbage>`` to an
    UNKNOWN project must return 401 — NOT the 404 the resolve would
    otherwise raise. The router can't validate a bearer itself, so a
    junk-bearer caller is not-yet-authenticated and must not be able to
    tell unknown from known by status-code differencing.

    (Pre-fix this returned 404, which — paired with a known project's
    401 — was exactly the enumeration oracle.)
    """
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/does-not-exist",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer some-junk-token",
            "Content-Type": "application/json",
        },
        allow_redirects=False,
    )

    assert resp.status == 401, await resp.text()
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


async def test_authenticated_rest_unknown_project_still_404(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression guard: the auth-before-resolve hardening on the MCP
    transport must NOT blanket-401 the operator-session-gated REST path.
    An authenticated operator (the auto-attached sentinel cookie) that
    asks for an UNKNOWN project on a lifecycle/admin endpoint still gets
    a genuine 404 — that's the legit path the fix must leave intact, and
    an unauthenticated caller can't reach it (the middleware 401s first).
    """
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects/does-not-exist/client-config",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()


# ── ITEM 3: reason-phrase reflection ────────────────────────────────


async def test_unknown_project_reason_phrase_is_constant(
    aiohttp_client, router_app, register_project,
) -> None:
    """The REST 404 for an unknown project must not reflect the
    caller-supplied name into the HTTP status line (reason phrase)."""
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    marker = "sentinel-secret-name"
    resp = await client.get(
        f"/agent-mcp/api/router/projects/{marker}/client-config",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )

    assert resp.status == 404
    assert marker not in (resp.reason or ""), (
        f"reason phrase reflects the caller-supplied name: {resp.reason!r}"
    )


async def test_unknown_agent_reason_phrase_is_constant(
    aiohttp_client, router_app, register_project, router_module,
) -> None:
    """The 404 for an unknown ``?agent=`` on the wiring endpoint must not
    reflect the caller-supplied agent_id into the status line."""
    register_project("known-proj")
    # Seed the token cache so ``_resolve_agent_token`` resolves against a
    # known (empty-of-this-agent) map instead of hitting a real backend.
    router_module._agent_token_cache["known-proj"] = (
        9.9e18, {"tok-real": "RealAgent"},
    )
    client = await aiohttp_client(router_app)

    marker = "sentinel-secret-agent"
    resp = await client.get(
        f"/agent-mcp/api/router/projects/known-proj/client-config?agent={marker}",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
        allow_redirects=False,
    )

    assert resp.status == 404
    assert marker not in (resp.reason or ""), (
        f"reason phrase reflects the caller-supplied agent_id: {resp.reason!r}"
    )


@pytest.mark.no_auth_seed_session
async def test_mcp_wrong_method_reason_phrase_is_constant(
    aiohttp_client, router_app, register_project,
) -> None:
    """The 405 for a disallowed HTTP verb on /mcp must not reflect the
    caller-supplied project name into the status line."""
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    resp = await client.request(
        "PUT",
        "/agent-mcp/mcp/known-proj",
        data=b"{}",
        headers={"Authorization": "Bearer some-junk-token"},
        allow_redirects=False,
    )

    assert resp.status == 405
    assert "known-proj" not in (resp.reason or ""), (
        f"reason phrase reflects the project name: {resp.reason!r}"
    )


# ── ITEM 4: pre-auth 401-ENVELOPE parity (SEC5, still open) ─────────
#
# PR #279 unified the pre-auth 401 STATUS CODE (unknown project → 401,
# not 404) but NOT the 401 ENVELOPE. A junk bearer to a KNOWN project
# is forwarded to the backend, whose ``AuthHeaderMiddleware`` returns
# its own 401: JSON ``invalid_bearer`` body, a ``Server: uvicorn``
# header, and NO ``WWW-Authenticate``. The SAME junk bearer to an
# UNKNOWN project is short-circuited by the router's own
# ``_unauthorized()``: a ``Server: aiohttp``-flavoured header, a
# ``WWW-Authenticate`` challenge, a different body, a different length.
#
# Those 5 distinguishers (Server header, body, WWW-Authenticate
# presence, length, reason) let an ANONYMOUS attacker enumerate valid
# project names by diffing the two 401s. The fix normalises the
# backend's pre-auth 401 into the router's canonical envelope so the
# two are BYTE-INDISTINGUISHABLE to a not-yet-authenticated caller.


class _ProductionShaped401Backend:
    """UDS backend that mirrors production's divergent ``invalid_bearer``
    401 — the exact envelope ``agent_mcp.app.main_app`` emits.

    Distinctive on purpose: a ``Server: uvicorn`` header (the ASGI
    server's fingerprint), a JSON ``invalid_bearer`` body, and NO
    ``WWW-Authenticate``. These are the leaks the router must scrub so
    the KNOWN-project 401 can't be told apart from the UNKNOWN-project
    401.
    """

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._reject)
        return app

    async def _reject(self, req: web.Request) -> web.Response:
        await req.read()
        return web.Response(
            status=401,
            body=(
                b'{"error":"invalid_bearer","message":"Bearer token does '
                b'not match any active agent. Send Authorization: Bearer '
                b'<per-agent-token> on POST /mcp, or route the request '
                b'through the router so it can attach the signed '
                b'forwarding header."}'
            ),
            content_type="application/json",
            # Mimic uvicorn's server fingerprint. aiohttp's writer uses
            # ``setdefault`` for the Server header, so an explicit value
            # survives — exactly like the real backend behind uvicorn.
            headers={"Server": "uvicorn"},
        )


@pytest_asyncio.fixture
async def production_shaped_401_backend(
    router_module, router_env, systemctl_stub,
):
    """Register KNOWN project ``known-proj`` and stand up a backend on
    its UDS that returns production's divergent ``invalid_bearer`` 401.

    Marks the systemd unit active so ``_ensure`` is a no-op and the
    junk-bearer request is forwarded to (and rejected by) this backend.
    """
    name = "known-proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws" / name))
    sock = router_env.sock_dir / name / "backend.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    backend = _ProductionShaped401Backend()
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock))
    await site.start()
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


def _envelope_fingerprint(resp, body: bytes) -> dict:
    """The security-relevant, deterministic 401 distinguishers.

    Excludes ``Date`` (clock-dependent) and ``Content-Type`` charset
    noise is kept as-is because it IS an observable distinguisher. The
    two 401s must agree on every field here.
    """
    return {
        "status": resp.status,
        "server": resp.headers.get("Server"),
        "www_authenticate": resp.headers.get("WWW-Authenticate"),
        "content_type": resp.headers.get("Content-Type"),
        "content_length": resp.headers.get("Content-Length"),
        "body": body,
        "reason": resp.reason,
    }


@pytest.mark.no_auth_seed_session
async def test_junk_bearer_401_envelope_indistinguishable_known_vs_unknown(
    aiohttp_client, router_app, production_shaped_401_backend,
) -> None:
    """SEC5 (still open): a junk ``Authorization: Bearer <garbage>`` to a
    KNOWN project (forwarded → backend 401) and to an UNKNOWN project
    (router ``_unauthorized()``) must be BYTE-INDISTINGUISHABLE.

    Pre-fix the KNOWN 401 leaks ``Server: uvicorn``, the JSON
    ``invalid_bearer`` body, no ``WWW-Authenticate``, and a ~230-byte
    length — none of which the UNKNOWN 401 has. That divergence is the
    project-existence oracle an anonymous attacker exploits.
    """
    client = await aiohttp_client(router_app)

    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    hdrs = {
        "Authorization": "Bearer some-junk-token",
        "Content-Type": "application/json",
    }

    resp_known = await client.post(
        "/agent-mcp/mcp/known-proj", data=body, headers=hdrs,
        allow_redirects=False,
    )
    known_body = await resp_known.read()
    resp_unknown = await client.post(
        "/agent-mcp/mcp/does-not-exist", data=body, headers=hdrs,
        allow_redirects=False,
    )
    unknown_body = await resp_unknown.read()

    fp_known = _envelope_fingerprint(resp_known, known_body)
    fp_unknown = _envelope_fingerprint(resp_unknown, unknown_body)

    assert fp_known == fp_unknown, (
        "401-envelope oracle: a junk bearer to a KNOWN project produced a "
        f"different 401 envelope than to an UNKNOWN project.\n"
        f"  known:   {fp_known}\n"
        f"  unknown: {fp_unknown}"
    )
    # The uvicorn fingerprint in particular must be scrubbed.
    assert fp_known["server"] != "uvicorn", (
        "backend 'Server: uvicorn' header leaked through the proxy on the "
        "pre-auth 401 path — fingerprints the ASGI server AND diverges "
        "from the router's own 401"
    )
    # The scrubbed 401 keeps the legitimate auth-challenge UX.
    assert "Bearer" in (fp_known["www_authenticate"] or ""), (
        "the normalised 401 must still carry a WWW-Authenticate challenge"
    )
