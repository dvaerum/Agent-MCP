"""SEC round-2 — residual pre-auth oracles on the /mcp transport.

Owner-authorised defensive review (2026-07-09). Two residual
project-existence oracles left open after PR #279 unified the pre-auth
401 STATUS + ENVELOPE:

FINDING 1 [MED] — TIMING oracle
  A junk ``Authorization: Bearer <garbage>`` to an UNKNOWN project
  collapses to 401 in-process (~fast); the SAME junk bearer to a KNOWN
  project is forwarded and incurs a full backend UDS round-trip before
  ITS 401 collapses (~slow). The latency delta let a not-yet-
  authenticated caller enumerate valid project names by timing. The
  handler now floors every pre-auth 401 to a fixed wall-clock target so
  known and unknown return at ~the same time.

FINDING 2 [LOW-MED] — 413 oracle
  An oversized body raises 413 only for a KNOWN project (the body is
  read inside ``_proxy_to_backend``); an UNKNOWN project 401s before any
  read. 413-vs-401 was therefore a project-existence oracle. The handler
  now collapses the 413 into the same uniform pre-auth 401.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from aiohttp import web


pytestmark = [pytest.mark.asyncio, pytest.mark.no_auth_seed_session]


class _Known401Backend:
    """UDS backend that always 401s after reading the body — models a
    KNOWN project whose backend rejects the (unvalidated) junk bearer."""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._reject)
        return app

    async def _reject(self, req: web.Request) -> web.Response:
        await req.read()
        return web.Response(
            status=401,
            body=b'{"error":"invalid_bearer"}',
            content_type="application/json",
            headers={"Server": "uvicorn"},
        )


@pytest_asyncio.fixture
async def known_401_backend(router_module, router_env, systemctl_stub):
    """Register KNOWN project ``known-proj`` with a live UDS backend that
    401s, and mark its unit active so ``_ensure`` is a no-op — the junk
    bearer is forwarded to (and rejected by) this backend, paying the
    round-trip cost the timing floor must absorb."""
    name = "known-proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws" / name))
    sock = router_env.sock_dir / name / "backend.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    backend = _Known401Backend()
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock))
    await site.start()
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


_JUNK = {"Authorization": "Bearer some-junk-token", "Content-Type": "application/json"}
_SMALL = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'


# ── FINDING 1: timing floor ─────────────────────────────────────────


async def test_preauth_401_timing_floor_known_vs_unknown(
    aiohttp_client, router_app, router_module, known_401_backend, monkeypatch,
) -> None:
    """A junk-bearer POST to a KNOWN project (backend round-trip) and to
    an UNKNOWN project (in-process) must both return no earlier than the
    pre-auth timing floor — otherwise the latency delta enumerates valid
    project names.

    Pre-fix: unknown returns in ~ms and known in ~14 ms, both well under
    the floor, so both floor assertions fail (RED).
    """
    monkeypatch.setattr(
        router_module, "_PREAUTH_401_FLOOR_SEC", 0.3, raising=False,
    )
    client = await aiohttp_client(router_app)

    t = time.monotonic()
    r_known = await client.post(
        "/agent-mcp/mcp/known-proj", data=_SMALL, headers=_JUNK,
        allow_redirects=False,
    )
    known = time.monotonic() - t

    t = time.monotonic()
    r_unknown = await client.post(
        "/agent-mcp/mcp/does-not-exist", data=_SMALL, headers=_JUNK,
        allow_redirects=False,
    )
    unknown = time.monotonic() - t

    assert r_known.status == 401, await r_known.text()
    assert r_unknown.status == 401, await r_unknown.text()
    # Both must clear the floor (allow small scheduling slack).
    assert known >= 0.25, f"known 401 returned in {known:.3f}s (< floor)"
    assert unknown >= 0.25, f"unknown 401 returned in {unknown:.3f}s (< floor)"
    # And the two must be timing-indistinguishable (delta dominated by
    # the shared floor, not the backend round-trip).
    assert abs(known - unknown) < 0.15, (
        f"timing oracle: known={known:.3f}s unknown={unknown:.3f}s "
        f"delta={abs(known - unknown):.3f}s"
    )


async def test_preauth_floor_keeps_www_authenticate(
    aiohttp_client, router_app, router_module, known_401_backend, monkeypatch,
) -> None:
    """The floored 401 must still carry the WWW-Authenticate challenge —
    the hardening may not degrade the legitimate auth-challenge UX."""
    monkeypatch.setattr(
        router_module, "_PREAUTH_401_FLOOR_SEC", 0.0, raising=False,
    )
    client = await aiohttp_client(router_app)
    for project in ("known-proj", "does-not-exist"):
        resp = await client.post(
            f"/agent-mcp/mcp/{project}", data=_SMALL, headers=_JUNK,
            allow_redirects=False,
        )
        assert resp.status == 401
        assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


# ── FINDING 2: oversized-body 413 collapse ──────────────────────────


async def test_oversized_body_413_collapsed_to_uniform_401(
    aiohttp_client, router_app, router_module, known_401_backend, monkeypatch,
) -> None:
    """An oversized body to a KNOWN project (413 at body read) and to an
    UNKNOWN project (401 before read) must return the SAME status.

    Pre-fix: known → 413, unknown → 401 (RED). Post-fix: both 401.
    """
    monkeypatch.setattr(
        router_module, "_PREAUTH_401_FLOOR_SEC", 0.0, raising=False,
    )
    client = await aiohttp_client(router_app)

    big = b'{"x":"' + b"a" * (router_module._MCP_MAX_BODY_BYTES + 1024) + b'"}'

    r_known = await client.post(
        "/agent-mcp/mcp/known-proj", data=big, headers=_JUNK,
        allow_redirects=False,
    )
    r_unknown = await client.post(
        "/agent-mcp/mcp/does-not-exist", data=big, headers=_JUNK,
        allow_redirects=False,
    )

    assert r_known.status == 401, (
        f"oversized body to KNOWN project returned {r_known.status}, "
        f"not the uniform 401 (413 oracle)"
    )
    assert r_unknown.status == 401, await r_unknown.text()
    assert r_known.status == r_unknown.status
