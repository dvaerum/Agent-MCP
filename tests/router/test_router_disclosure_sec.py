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
    differencing (unknown→404 vs known→401). The handler now gates on
    credential PRESENCE before resolving, so an anonymous probe gets a
    uniform 401 either way. Authenticated callers still get a genuine
    404 for a truly-unknown project.
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


async def test_authenticated_mcp_unknown_project_still_404(
    aiohttp_client, router_app, register_project,
) -> None:
    """Regression guard: a caller that DOES present a credential (here a
    bearer) still gets a genuine 404 for a truly-unknown project — the
    auth-before-resolve reorder must not blanket-401 authenticated
    callers, since the overview/lifecycle handlers rely on real 404
    semantics for unknown projects.

    A bearer to an UNKNOWN project resolves to 404 at the router (the
    project can't be found, so there's no backend to forward to); a
    bearer to a KNOWN project would forward to the backend (not tested
    here — no backend stood up).
    """
    register_project("known-proj")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/mcp/does-not-exist",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        headers={
            "Authorization": "Bearer some-agent-token",
            "Content-Type": "application/json",
        },
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()
