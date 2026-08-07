"""Round-4 SEC finding AC-race — request-scoped operator identity must
NOT be sourced from a process-global.

Background
----------
``AuthHeaderMiddleware`` used to stamp the forwarding-header operator id
onto ``g.current_operator`` (a plain module global) before
``await call_next(request)``. The FastAPI dep ``require_operator_session``
then read that global AFTER the await to build the audit ``user_id`` for
create / restore / edit / terminate / purge + settings writes.

Because it is a *process-wide* global and there is an ``await`` between
the middleware write and the dep read, two concurrent forwarding-header
requests (two dashboard operators reaching the same per-project backend)
interleave on the single-threaded event loop: operator B's dispatch
overwrites the global that operator A's dep is about to read, so A's
action is audit-logged under B's id (non-repudiation / audit-integrity
failure), and a legitimate request can read a clobbered value.

The fix resolves the forwarding operator from the per-request
``request.state.principal`` (built once per request by the middleware,
copy-per-task and race-safe) instead of the shared global.

These tests pin that contract:

* ``test_dep_reads_principal_not_process_global`` — deterministic: the
  dep returns the operator named by *this request's* principal even when
  the process-global holds a different (concurrent-request) value.
* ``test_concurrent_forwarding_requests_no_cross_attribution`` —
  interleaves two forwarding requests at the ``await call_next`` boundary
  and asserts each resolves ITS OWN operator, never the other's.
* ``test_single_forwarding_request_resolves_own_operator`` — regression:
  the ordinary single-request path still resolves the right operator.
* ``test_missing_principal_falls_through_forwarding_branch`` — a request
  with no forwarding principal does not spuriously resolve as forwarding
  (cookie / bearer paths stay reachable).
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from starlette.requests import Request

from agent_mcp.app import forwarding_header as _fh
from agent_mcp.app.deps import require_operator_session
from agent_mcp.app.main_app import AuthHeaderMiddleware
from agent_mcp.core import globals as g
from agent_mcp.core.principal import Principal
from tests.harness import make_principal

pytestmark = pytest.mark.asyncio


# ---------- helpers --------------------------------------------------


def _make_request(path: str = "/api/all-data", header_value: str | None = None) -> Request:
    """Build a minimal ASGI ``Request`` for the dep / middleware stack."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if header_value is not None:
        headers.append((_fh.HEADER_NAME.lower().encode(), header_value.encode()))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {},
    }

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


def _forwarding_principal(operator_id: str) -> Principal:
    return make_principal(
        kind="forwarding_header",
        user_id=operator_id,
        agent_id=None,
        sysadmin=False,
        project_name=None,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


@pytest.fixture
def hmac_key():
    """Set a process HMAC key for the middleware; restore afterwards."""
    prev = g.forwarding_hmac_key
    prev_op = getattr(g, "current_operator", None)
    g.forwarding_hmac_key = os.urandom(32)
    try:
        yield g.forwarding_hmac_key
    finally:
        g.forwarding_hmac_key = prev
        # Leave the diagnostic global as we found it so no state bleeds
        # into a co-located test in the same xdist worker.
        with contextlib.suppress(Exception):
            g.current_operator = prev_op


# ---------- deterministic: dep must read the principal ---------------


async def test_dep_reads_principal_not_process_global():
    """The dep resolves the forwarding operator from *this request's*
    ``request.state.principal``, NOT from the shared process-global.

    RED against origin/main: the dep reads ``g.current_operator``, so the
    poisoned value ("intruder", standing in for a concurrent request's
    leak) is returned. GREEN after the fix: the per-request principal
    ("realop") wins.
    """
    req = _make_request()
    req.state.principal = _forwarding_principal("realop")

    # Simulate a concurrent request having clobbered the process-global.
    with contextlib.suppress(Exception):
        g.current_operator = "intruder"

    auth = await require_operator_session(req)

    assert auth == {"kind": "forwarding", "operator_id": "realop"}
    # And the audit identifier derived from it is the request's own.
    from agent_mcp.app.deps import caller_identity

    assert caller_identity(auth) == "realop"


# ---------- concurrency: interleave at the await boundary ------------


async def test_concurrent_forwarding_requests_no_cross_attribution(hmac_key):
    """Two forwarding requests with DISTINCT operators, interleaved at the
    ``await call_next`` seam, must each resolve their own operator.

    The barrier releases both tasks only after BOTH have run the
    middleware write path, so on origin/main the process-global holds the
    last writer's id for BOTH dep reads → cross-attribution → RED. With
    per-request principals each dep reads its own identity → GREEN.
    """
    key = hmac_key
    mw = AuthHeaderMiddleware(app=lambda scope, receive, send: None)
    barrier = asyncio.Barrier(2)

    async def _call_next(request):
        # Both requests have completed the middleware identity write by
        # the time they reach here; block until both arrive so the
        # process-global (if used) is definitively the last writer's.
        await barrier.wait()
        return await require_operator_session(request)

    async def _run(operator_id: str):
        header = _fh.sign(operator_id, "operator", key, ttl_sec=30)
        req = _make_request(header_value=header)
        return await mw.dispatch(req, _call_next)

    results = await asyncio.gather(_run("alice"), _run("bob"))

    assert results[0] == {"kind": "forwarding", "operator_id": "alice"}
    assert results[1] == {"kind": "forwarding", "operator_id": "bob"}


# ---------- regressions ----------------------------------------------


async def test_single_forwarding_request_resolves_own_operator(hmac_key):
    """A single forwarding request (no concurrency) still resolves the
    correct operator through the middleware + dep stack."""
    key = hmac_key
    mw = AuthHeaderMiddleware(app=lambda scope, receive, send: None)

    async def _call_next(request):
        return await require_operator_session(request)

    header = _fh.sign("carol", "operator", key, ttl_sec=30)
    req = _make_request(header_value=header)
    result = await mw.dispatch(req, _call_next)

    assert result == {"kind": "forwarding", "operator_id": "carol"}


async def test_missing_principal_falls_through_forwarding_branch():
    """When no forwarding principal is present the dep does NOT resolve as
    forwarding — it falls through to the bearer / body / query paths (and,
    with none of those, raises 401). This keeps the cookie + bearer paths
    reachable and proves the forwarding branch is gated on the principal,
    not on a stale global."""
    from fastapi import HTTPException

    req = _make_request()  # no principal, no forwarding header
    # A poisoned global must NOT make this resolve as forwarding.
    with contextlib.suppress(Exception):
        g.current_operator = "ghost"

    with pytest.raises(HTTPException) as excinfo:
        await require_operator_session(req)

    assert excinfo.value.status_code == 401
