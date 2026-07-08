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
