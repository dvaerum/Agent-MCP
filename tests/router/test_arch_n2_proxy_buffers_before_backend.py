"""N2 / FLAG-R7-1: the router materialises the WHOLE client body before
it opens the backend connection — the load-bearing invariant that keeps
the OBS-R11-1 stale-Principal class closed on the backend REST surface.

Why this file exists
--------------------
``test_arch_enforced_revalidation.py`` structurally enforces "no bare
yield point ahead of revalidation" on ONE of this system's three
request surfaces — the aiohttp router admin API (``agent_mcp/router/``),
whose handlers fuse the body-read and the re-check through
``perm_gates.read_body_and_revalidate`` / ``revalidated_lock`` /
``revalidate_after``. The other two surfaces have the SAME code shape
but no fused helper of their own:

* **Backend REST** (FastAPI, ``agent_mcp/app/routers/``, 40+ handlers):
  every one resolves ``Depends(require_operator_session)`` first, then
  does ``await get_sanitized_json_body(request)`` (a body read), then
  writes — textbook check → yield → act-on-stale-check.
* **MCP tools** (``agent_mcp/tools/``, 49 registered): the gate runs in
  ``dispatch_tool_call`` before the impl is entered, then the impl
  awaits (DB offload, RAG embedding) and writes. See
  ``tests/test_arch_n2_tool_surface_yield_points.py`` for that surface's
  own invariant.

Round-7's pentest lane (ledger id ``FLAG-R7-1``) live-tested the backend
REST shape — a slow-drip ``DELETE /api/<project>/messages/<id>`` racing
a concurrent role demotion — and found it correctly denied. The reason
is NOT that any backend handler re-validates: ``grep -ri revalidat
agent_mcp/app/`` finds nothing outside the two SSE keep-alive loops
(``events.py`` / ``delivery.py``). The reason is that
``router/app.py::_proxy_to_backend`` fully materialises the client body
(``req_body = await req.read()``) BEFORE it opens the ``ClientSession``
to the backend's Unix socket, so the entire attacker-paced slow-drip
window is consumed at the ROUTER layer — the backend's own fresh
``require_operator_session`` doesn't even run until the last byte of the
client's body has already arrived. The stale-Principal window that
remains on the backend side is the microseconds it takes to write a
buffered ``bytes`` object down a UDS, which no caller can stretch.

FLAG-R7-1's own words: *"This is an INCIDENTAL side effect of the
proxy's current buffer-then-forward implementation, not a designed/
tested invariant — if ``_proxy_to_backend`` is ever changed to stream
the body incrementally (e.g. for large uploads), this whole class
re-opens in one move across every FastAPI-backend route."* It closed
with a recommendation for exactly this file: pin the dependency so a
future streaming refactor fails loudly instead of silently reopening
the class across 40+ handlers with every existing test still green.

What is (and is not) claimed
----------------------------
The claim is narrow and deployment-scoped: **when the backend is reached
through the router — which is the only way it is reached in the shipped
systemd deployment, where the backend binds a Unix socket the router
owns — no caller can hold a backend REST handler suspended between its
auth dependency and its write.** It is NOT a claim that the backend
handlers are individually safe. A backend reachable directly (the
"co-located systemd backend reachable directly" misconfiguration
posture that ``app/deps.py::_backend_project_name`` already hardens
against defensively) has no buffering in front of it and the class is
open there. Closing THAT would mean a per-handler re-validation adapter
across 40+ FastAPI routes, which needs Finding D's typed ``Principal``
first — deliberately out of scope here, and recorded as such rather
than silently assumed away.

The four tests below pin the invariant from both directions:

* behaviourally, end-to-end through the real router app + a real
  UDS-bound backend: the backend must observe NOTHING while a client's
  body read is paused (``test_backend_sees_nothing_until_client_body_
  is_complete``);
* structurally, by AST, so the reason is also visible to a reader of
  ``_proxy_to_backend`` and cannot be defeated by a refactor that keeps
  the behaviour test's particular timing happy.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` — three of
# the four tests below are plain synchronous AST walks and pytest-asyncio
# warns on a sync test carrying the mark. The one async test opts in
# individually, matching this suite's convention (see
# ``test_arch_enforced_revalidation.py``).


# ── Backend stand-in (same shape as test_proxy_passthrough.py) ───────


class _RecordingBackend:
    """UDS-bound aiohttp app that records every request it receives.

    Deliberately records on ENTRY (before reading the body) as well as
    after: this file's whole point is *when* the backend first hears
    about a request, so "the handler was entered" is the event that
    matters, not "the handler finished reading".
    """

    def __init__(self) -> None:
        self.entered: list[dict] = []
        self.records: list[dict] = []
        self.response_factory: (
            Callable[[web.Request], Awaitable[web.Response]] | None
        ) = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def _handle(self, req: web.Request) -> web.Response:
        self.entered.append({"method": req.method, "path": req.path})
        body = await req.read()
        self.records.append(
            {"method": req.method, "path": req.path, "body": body},
        )
        if self.response_factory is not None:
            return await self.response_factory(req)
        return web.Response(body=b"OK")


async def _start_backend_on_uds(
    backend: _RecordingBackend, sock_path: Path,
) -> web.AppRunner:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


@pytest_asyncio.fixture
async def recording_backend(router_module, router_env, systemctl_stub):
    """A UDS backend for project ``proj``, unit marked already-active so
    ``_ensure`` doesn't try to start anything."""
    name = "proj"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock = router_env.sock_dir / name / "backend.sock"
    backend = _RecordingBackend()
    runner = await _start_backend_on_uds(backend, sock)
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    try:
        yield backend
    finally:
        await runner.cleanup()


# ── The behavioural invariant ────────────────────────────────────────


@pytest.mark.asyncio
async def test_backend_sees_nothing_until_client_body_is_complete(
    aiohttp_client, router_app, recording_backend, router_module, monkeypatch,
) -> None:
    """FLAG-R7-1's invariant, end-to-end.

    A client opens a proxied request and stalls mid-body (simulated
    deterministically by pausing ``web.Request.read`` — the same
    mechanism ``test_sec_r7f3_mcp_forwarding_header_toctou.py`` uses to
    reproduce a slow-drip without real sleeps). While it is stalled the
    backend must not have been entered AT ALL: not the handler, not the
    auth dependency it would resolve, not a socket connection.

    If ``_proxy_to_backend`` is ever refactored to stream the client
    body upstream (``data=req.content`` or an async iterator), the
    backend is entered — and its ``Depends(require_operator_session)``
    resolved — while the client still controls the pace of the
    remaining body. That is precisely the OBS-R11-1 window, reopened
    across every FastAPI backend route at once. This test goes red the
    moment that happens.
    """
    router_module._agent_token_cache["proj"] = (9.9e18, {"tok-1234": "Admin"})
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    client = await aiohttp_client(router_app)

    body_read_started = asyncio.Event()
    release_body_read = asyncio.Event()
    paused = {"done": False}
    original_read = web.Request.read
    target_path = "/agent-mcp/mcp/proj"

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

    task = asyncio.ensure_future(
        client.post(
            target_path,
            data=payload,
            headers={
                "Authorization": "Bearer tok-1234",
                "Content-Type": "application/json",
            },
        ),
    )
    await asyncio.wait_for(body_read_started.wait(), timeout=5)

    # Give the event loop a few turns: if anything upstream were going
    # to be opened concurrently with the stalled read, it would happen
    # here. A single ``await`` could pass vacuously.
    for _ in range(10):
        await asyncio.sleep(0)

    assert recording_backend.entered == [], (
        "the backend was entered while the client's request body was "
        "still in flight — ``_proxy_to_backend`` is no longer "
        "buffer-then-forward. Every FastAPI handler under "
        "agent_mcp/app/routers/ resolves "
        "``Depends(require_operator_session)`` and THEN awaits "
        "``get_sanitized_json_body(request)``; with a streamed body "
        "that await becomes caller-paced again and the OBS-R11-1 "
        "stale-Principal class (FLAG-R7-1) is reopened across 40+ "
        "routes at once. Either restore the full ``await req.read()`` "
        "ahead of the upstream connection, or give the backend surface "
        "its own re-validation seam first — see this module's docstring."
    )

    release_body_read.set()
    resp = await asyncio.wait_for(task, timeout=5)
    assert resp.status == 200, await resp.text()

    assert len(recording_backend.records) == 1, (
        f"expected exactly one proxied request after the body "
        f"completed; got {recording_backend.records!r}"
    )
    assert recording_backend.records[0]["body"] == payload, (
        "the backend must receive the complete body in one shot"
    )


# ── The structural invariant (AST) ───────────────────────────────────


def _router_app_tree() -> ast.Module:
    """AST for ``agent_mcp/router/app.py``, located via the PACKAGE's
    ``__file__`` and read from disk.

    Deliberately does not ``import agent_mcp.router.app``: that module
    reads ``AGENT_MCP_SOCK_DIR`` at import time (via
    ``project_orchestrator``), which only the ``router_env`` fixture
    sets. Importing it here would make these pure static checks pass or
    fail depending on whether an unrelated fixture happened to run
    first in the same worker — the sort of order dependence xdist
    surfaces at random.
    """
    import agent_mcp.router as _pkg

    return ast.parse((Path(_pkg.__file__).parent / "app.py").read_text())


def _find_func(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name!r} no longer exists in agent_mcp/router/app.py — this "
        f"file's invariant is about that function specifically; if it "
        f"was renamed or split, re-point these tests rather than "
        f"deleting them."
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _buffered_body_names(func: ast.AsyncFunctionDef) -> dict[str, int]:
    """``{target_name: lineno}`` for every ``x = await <req>.read()`` in
    ``func`` — the full-body materialisation this invariant is about."""
    out: dict[str, int] = {}
    for node in ast.walk(func):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and _call_name(node.value.value.func) == "read"
        ):
            continue
        out[node.targets[0].id] = node.lineno
    return out


def _upstream_open_linenos(func: ast.AsyncFunctionDef) -> list[int]:
    """Line numbers of every call in ``func`` that opens (or issues) the
    upstream connection: ``ClientSession(...)`` and ``sess.request(...)``.
    """
    out: list[int] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _call_name(node.func) in (
            "ClientSession", "request",
        ):
            out.append(node.lineno)
    return sorted(out)


def test_proxy_reads_the_body_before_opening_the_upstream() -> None:
    """Structural half of the behavioural test above: the body-read
    yield point must precede every upstream-opening call in source
    order, so the ordering is visible to a reader (and to review) and
    not merely an accident of the current control flow."""
    func = _find_func(_router_app_tree(), "_proxy_to_backend")
    buffered = _buffered_body_names(func)
    assert buffered, (
        "_proxy_to_backend no longer contains an ``x = await "
        "<req>.read()`` full-body materialisation. See FLAG-R7-1 / this "
        "module's docstring: 40+ FastAPI backend handlers depend on that "
        "read consuming the caller-paced window BEFORE the backend is "
        "reached."
    )
    opens = _upstream_open_linenos(func)
    assert opens, (
        "no ClientSession(...)/sess.request(...) call found in "
        "_proxy_to_backend — the AST detector has stopped matching and "
        "the ordering assertion below would pass vacuously."
    )
    first_read = min(buffered.values())
    first_open = min(opens)
    assert first_read < first_open, (
        f"_proxy_to_backend opens the upstream connection at line "
        f"{first_open}, BEFORE it materialises the client body at line "
        f"{first_read}. That hands the backend a request whose remaining "
        f"body is still paced by the caller, reopening FLAG-R7-1's "
        f"stale-Principal window on every backend REST handler."
    )


def test_proxy_forwards_the_buffered_bytes_not_a_stream() -> None:
    """The upstream request's ``data=`` must be the buffered ``bytes``
    name bound by the body read — not ``req.content``, not an async
    iterator, not the request object itself.

    This is the check that survives a refactor which keeps the read in
    place "for validation" but then streams the real body anyway: the
    read alone is not the invariant, forwarding THE READ RESULT is.
    """
    func = _find_func(_router_app_tree(), "_proxy_to_backend")
    buffered = _buffered_body_names(func)
    data_kwargs = [
        kw
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "data"
    ]
    assert len(data_kwargs) == 1, (
        f"expected exactly one ``data=`` kwarg in _proxy_to_backend "
        f"(the upstream request's body); found {len(data_kwargs)}. "
        f"Re-point this test if the proxy legitimately grew a second "
        f"upstream call."
    )
    value = data_kwargs[0].value
    assert isinstance(value, ast.Name), (
        f"_proxy_to_backend forwards ``data={ast.dump(value)}`` — the "
        f"invariant requires forwarding the already-buffered bytes, so "
        f"``data=`` must be a plain name bound by ``await "
        f"<req>.read()``. A stream/iterator here reopens FLAG-R7-1."
    )
    assert value.id in buffered, (
        f"_proxy_to_backend forwards ``data={value.id}``, which is not "
        f"bound by an ``await <req>.read()`` in this function "
        f"(buffered names: {sorted(buffered)!r}). Forwarding anything "
        f"other than the fully-materialised body reopens FLAG-R7-1."
    )


def test_proxy_to_backend_is_the_only_client_body_forwarding_seam() -> None:
    """Nothing else in ``agent_mcp/router/`` may forward a request body
    upstream.

    The invariant above is worth exactly as much as the claim that every
    backend-bound request goes through the one function it constrains.
    Both proxy entry points (``backend_api_handler`` for the whole
    ``/api/<project>/...`` REST surface and ``backend_mcp_handler`` for
    ``/mcp``) call ``_proxy_to_backend``; the only other upstream client
    in the package (``_agent_token_map``'s ``GET /api/tokens``) sends no
    body at all. A second, hand-rolled forwarding path would bypass the
    buffering silently — the same "a future third file gets the same
    shape and nobody adds it to the list" failure mode Finding G fixed
    in the revalidation detector's own plumbing, which is why this is
    discovered rather than listed.
    """
    import agent_mcp.router as _pkg

    # aiohttp client-side verbs. Matched on the attribute name only —
    # the receiver is a session object bound several lines earlier, so
    # resolving it statically would be guesswork; a false positive here
    # is a prompt to read the new call site, which is the intent.
    client_verbs = {
        "request", "get", "post", "put", "patch", "delete", "head",
        "options", "ws_connect",
    }
    offenders: list[str] = []
    for path in sorted(Path(_pkg.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            if node.name == "_proxy_to_backend":
                continue
            for inner in ast.walk(node):
                if not (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in client_verbs
                ):
                    continue
                if any(kw.arg in ("data", "json") for kw in inner.keywords):
                    offenders.append(
                        f"{path.name}:{node.name}:{inner.lineno}",
                    )
    assert not offenders, (
        f"found upstream call(s) forwarding a body outside "
        f"_proxy_to_backend: {offenders!r}. Every backend-bound request "
        f"must go through the one buffer-then-forward seam — a second "
        f"forwarding path would bypass FLAG-R7-1's invariant without "
        f"tripping any of the checks above. If this is a legitimate new "
        f"seam, it must buffer the body the same way and this test must "
        f"be widened deliberately, not silenced."
    )
