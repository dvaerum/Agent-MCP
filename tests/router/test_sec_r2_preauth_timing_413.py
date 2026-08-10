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


async def test_preauth_401_known_and_unknown_both_routed_through_floor(
    aiohttp_client, router_app, router_module, known_401_backend, monkeypatch,
) -> None:
    """SEC5 timing-oracle closure, asserted by BEHAVIOUR — not by a
    measured wall-clock threshold.

    The invariant: a junk-bearer POST to a KNOWN project (which pays a
    full backend UDS round-trip before its 401) and to an UNKNOWN
    project (which 401s in-process) must BOTH funnel their 401 through
    the single ``_floored_unauthorized`` gate. That shared gate —
    anchored at handler-entry ``t0`` — is what makes the two paths
    timing-indistinguishable; if the KNOWN path ever returned the
    backend's raw 401 without the floor (or the UNKNOWN path skipped
    it), the latency delta would re-open project-name enumeration.

    R16-1: the previous form asserted ``abs(known - unknown) < 0.15`` on
    measured wall-clock, which jittered under ``pytest -n auto`` (the
    delta is scheduling noise, not the round-trip). Spying the shared
    gate proves the same invariant deterministically, and still goes RED
    if either path stops flooring.
    """
    calls: list[float] = []
    original = router_module._floored_unauthorized

    async def _spy(t0: float):
        calls.append(t0)
        return await original(t0)

    # Floor value is irrelevant to a call-based assertion; zero it so the
    # test does no real sleeping.
    monkeypatch.setattr(
        router_module, "_PREAUTH_401_FLOOR_SEC", 0.0, raising=False,
    )
    monkeypatch.setattr(router_module, "_floored_unauthorized", _spy)
    client = await aiohttp_client(router_app)

    r_known = await client.post(
        "/agent-mcp/mcp/known-proj", data=_SMALL, headers=_JUNK,
        allow_redirects=False,
    )
    known_calls = len(calls)
    r_unknown = await client.post(
        "/agent-mcp/mcp/does-not-exist", data=_SMALL, headers=_JUNK,
        allow_redirects=False,
    )

    assert r_known.status == 401, await r_known.text()
    assert r_unknown.status == 401, await r_unknown.text()
    # KNOWN path (backend round-trip) floored its 401 …
    assert known_calls == 1, (
        "known-project junk bearer did not route its 401 through the "
        "timing floor — the backend round-trip latency stays observable"
    )
    # … and UNKNOWN path (in-process) floored its 401 too.
    assert len(calls) == 2, (
        "unknown-project junk bearer did not route its 401 through the "
        "timing floor"
    )


async def test_floored_unauthorized_absorbs_elapsed_since_t0(
    router_module, monkeypatch,
) -> None:
    """The floor ABSORBS (rather than adds to) time already spent since
    handler entry: ``_floored_unauthorized`` sleeps only for the
    REMAINDER of ``_PREAUTH_401_FLOOR_SEC`` measured from ``t0``.

    This is *why* a KNOWN project's backend round-trip is hidden inside
    the fixed window instead of stacking on top of it — the mechanism
    that makes known vs unknown timing-indistinguishable. Asserted on
    the computed sleep argument (deterministic), never on measured
    wall-clock, so it can't jitter under ``-n auto``; it still goes RED
    if the floor stops anchoring at ``t0``.
    """
    slept: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    monkeypatch.setattr(
        router_module, "_PREAUTH_401_FLOOR_SEC", 10.0, raising=False,
    )
    monkeypatch.setattr(router_module.asyncio, "sleep", _fake_sleep)

    now = router_module.time.monotonic()
    # t0 == "handler entry just now" → sleep ≈ the full floor.
    await router_module._floored_unauthorized(now)
    # t0 that already spent ~4 s (e.g. a slow backend round-trip) →
    # sleep ≈ floor − 4 s. The round-trip is absorbed, not added.
    await router_module._floored_unauthorized(now - 4.0)

    assert len(slept) == 2, "floor did not compute a remaining sleep"
    fresh, after_roundtrip = slept[0], slept[1]
    assert 9.5 <= fresh <= 10.0, fresh
    assert 5.5 <= after_roundtrip <= 6.0, after_roundtrip
    assert after_roundtrip < fresh - 3.0, (
        "floor did not absorb elapsed-since-t0 — a backend round-trip "
        "would stack on top of the floor and re-open the timing oracle"
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
