"""N2: the MCP tool surface's half of the OBS-R11-1 invariant.

``tests/router/test_arch_enforced_revalidation.py`` enforces "no bare
yield point ahead of revalidation" on the aiohttp router admin surface,
where handlers fuse the body-read and the re-check through
``perm_gates.read_body_and_revalidate`` / ``revalidated_lock`` /
``revalidate_after``. That test globs ``agent_mcp/router/*.py`` only —
one of THREE request surfaces with the same check → yield → write shape.
This module covers the third one (49 registered MCP tools); the second
(40+ FastAPI backend REST handlers) is covered by
``tests/router/test_arch_n2_proxy_buffers_before_backend.py``.

Why the tools surface needs a DIFFERENT invariant, not a copy
-------------------------------------------------------------
The router surface is exploitable-in-principle because its yield point
is **caller-paced**: ``await req.read()`` suspends the handler for as
long as a slow-drip client cares to hold the socket open, which is the
window every recurrence of this class (R6-F2 → R7-F1 → R7-F3 → R8-F3 →
R9-F2/F3/F4 → R13-F1 → R14-F2) actually used. Its fix is a fused helper
that re-derives authority the instant the await resolves.

Mechanically copying that rule onto ``agent_mcp/tools/`` would be
wrong. A survey of all 49 registered tools finds 16 with a yield point
at all, and every one of those awaits an *internal* async helper
(``_update_single_task``, ``emit_context_write_wakes``,
``validate_task_placement``, ``query_rag_system``, …) — none of them is
a handle whose pace the caller controls. Demanding a re-validation
marker after each of those would mean building a 49-tool re-validation
adapter to close a window no caller can open, and would change what is
actually enforced. That is the "naive reading would have been a
regression" trap this plan has already hit twice (Phase 1's N3
subtraction, Phase 2's registration-vs-decorator enforcement question),
so this file pins the property that is genuinely load-bearing instead:

  **The tool surface cannot have a caller-paced yield point at all.**

Two structural facts make that true, and both are checked below:

1. ``dispatch_tool_call`` (``tools/registry.py``) is handed an
   already-materialised ``dict`` of arguments. It resolves the caller's
   Principal, runs the authorization gate (R20-F4/R21-F1's pre-schema
   ``check_capability_gate`` / ``check_policy_gate`` /
   ``check_predicate_gate``, reading exactly the stamps Phase 2's
   ``requires=`` declaration verifies), validates the schema, and calls
   the implementation — with **zero yield points anywhere before the
   implementation is entered**. Nothing can be revoked "between the
   gate and the call" because there is no between. This is
   ``perm_gates``' fusion property arrived at by construction rather
   than by a helper, and it is exactly as fragile: one ``await`` added
   in that stretch (an async audit-log write, an async rate-limit
   probe) reopens the gap silently. ``test_no_yield_point_before_the_
   authorization_gate`` and ``test_no_yield_point_between_gate_and_
   tool_invocation`` are the guard.

2. No tool implementation can *acquire* a caller-paced handle: the
   whole ``agent_mcp/tools/`` package references no HTTP request type,
   no request/response stream, and no socket — the tool layer's only
   input is the decoded ``dict``. ``test_tool_carries_no_caller_paced_
   yield_primitive`` checks this per registered tool (the same
   discovered-target parametrization the router test uses, 49 cases
   instead of 16), and ``test_tools_package_declares_no_transport_
   dependency`` checks the whole package so nothing can hide in a
   helper the per-tool walk doesn't reach.

Known, tracked exception
------------------------
``wait_for_events`` (``agent_communication_tools.py``) genuinely holds
indefinitely — it is a long-lived wake-loop stream, not a
check-yield-write handler, and it re-validates on its own heartbeat
cadence. It is one of the four independently-implemented stream
re-validation loops the hardening plan's **N5** item exists to fuse
(``events.py``, ``delivery.py``, ``main_app.py``'s SSE pump, and this
one). Stream lifetimes are N5's subject, not this file's; the checks
below are about the request lifecycle.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import agent_mcp.tools  # noqa: F401 — import for side effect: registers tools
from agent_mcp.tools.registry import tool_implementations

# Transport / stream dependencies the tool layer must never grow. Each
# one is a way to obtain an object whose read pace an external caller
# controls — the ONLY primitive that turns "check, await, write" from a
# shape into an exploit. Tool arguments arrive fully decoded, so a tool
# has no legitimate reason to reach for any of these.
_TRANSPORT_ROOT_MODULES = frozenset({
    "aiohttp",
    "starlette",
    "fastapi",
    "httpx",
    "requests",
    "socket",
    "socketserver",
    "ssl",
    "http",
    "urllib",
    "websockets",
    "asyncio.streams",
})

# Method names that read from a caller-paced source. ``read`` is
# deliberately ABSENT: ``Path.read_text`` / file-object ``.read()`` are
# ordinary local I/O used by the file tools, and banning the bare name
# would be noise rather than signal. The names below have no
# local-filesystem meaning — every one of them belongs to an HTTP
# request/response or a stream reader.
_CALLER_PACED_READ_METHODS = frozenset({
    "body",
    "stream",
    "form",
    "iter_any",
    "iter_chunked",
    "iter_chunks",
    "iter_lines",
    "receive",
    "readexactly",
    "readuntil",
    "content_read",
})


def _tools_package_dir() -> Path:
    """Filesystem directory backing ``agent_mcp.tools``, resolved via the
    package's own ``__file__`` (not a path relative to this test file),
    matching ``test_arch_enforced_revalidation._router_package_dir``."""
    import agent_mcp.tools as _pkg

    return Path(_pkg.__file__).parent


def _transport_offenders(tree: ast.AST) -> list[str]:
    """Every reference in ``tree`` that could yield a caller-paced
    handle: an import of a transport module, or a call to one of the
    stream-read methods above."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _TRANSPORT_ROOT_MODULES:
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _TRANSPORT_ROOT_MODULES:
                offenders.append(
                    f"line {node.lineno}: from {node.module} import ...",
                )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CALLER_PACED_READ_METHODS
        ):
            offenders.append(f"line {node.lineno}: .{node.func.attr}()")
    return offenders


# ── Discovered targets: the LIVE registry, no hand-maintained list ───

_REGISTERED_TOOLS = sorted(tool_implementations)


def test_discovery_found_every_registered_tool() -> None:
    """Detector sanity guard, mirroring the router test's
    ``test_discovery_found_the_known_call_sites``: if the registry
    import side effect ever stops populating ``tool_implementations``,
    every parametrized case below would vacuously "pass" by not
    existing — worse than not having them.

    Phase 2 (Finding A) pinned the catalogue at 49 registered tools,
    each carrying a ``requires=`` declaration; this asserts the same
    order of magnitude without re-pinning the exact count (a new tool
    is expected to raise it, and Phase 2's own exact-set test is the
    place that notices additions).
    """
    assert len(_REGISTERED_TOOLS) >= 45, (
        f"only {len(_REGISTERED_TOOLS)} tools discovered in the live "
        f"registry: {_REGISTERED_TOOLS!r} — Phase 2 counted 49. The "
        f"registration side effect may have stopped firing, which "
        f"would make every parametrized case below check nothing."
    )
    for expected in (
        "assign_task",
        "update_task_status",
        "update_project_context",
        "ask_project_rag",
        "broadcast_admin_message",
        "wait_for_events",
    ):
        assert expected in tool_implementations, (
            f"{expected!r} is not in the live tool registry — the "
            f"discovery source for this file has changed shape."
        )


@pytest.mark.parametrize("tool_name", _REGISTERED_TOOLS)
def test_tool_carries_no_caller_paced_yield_primitive(tool_name: str) -> None:
    """No registered tool implementation may hold a handle whose read
    pace an external caller controls.

    This is the tools-surface equivalent of the router test's
    "immune by construction" branch, made structural: the router's
    handlers are immune only when they happen to have no yield point,
    whereas a tool is immune because the primitive that makes a yield
    point *dangerous* — a caller-paced source — cannot exist in this
    layer at all. Sixteen of these tools DO await; every one of those
    awaits an internal helper on a pace the caller cannot influence, so
    the entry-time authorization gate cannot be held stale.
    """
    impl = tool_implementations[tool_name]
    inner = inspect.unwrap(impl)
    source = textwrap.dedent(inspect.getsource(inner))
    offenders = _transport_offenders(ast.parse(source))
    assert not offenders, (
        f"tool {tool_name!r} ({inner.__module__}.{inner.__name__}) "
        f"references a transport/stream primitive: {offenders!r}. Tool "
        f"arguments arrive at dispatch_tool_call as an already-decoded "
        f"dict precisely so a tool body cannot suspend on a "
        f"caller-paced source; a handle like this reintroduces the "
        f"OBS-R11-1 window (authorization gate resolved at dispatch, "
        f"caller stalls the coroutine, privilege revoked, stale write "
        f"proceeds) on a surface that has no re-validation seam. If a "
        f"tool genuinely needs to talk to the network, put that behind "
        f"a feature module (as rag_tools does with features/rag) and "
        f"keep the transport handle out of the tool layer."
    )


def test_tools_package_declares_no_transport_dependency() -> None:
    """Whole-package sweep: the per-tool walk above only sees each
    entry point's own source, so a helper further down the module could
    still smuggle in a request handle. Every ``agent_mcp/tools/*.py``
    is checked here, discovered by walking the package directory (same
    idiom as the router test's ``_discover_target_modules``) rather
    than from a list someone has to remember to extend.
    """
    offenders: dict[str, list[str]] = {}
    scanned = 0
    for path in sorted(_tools_package_dir().glob("*.py")):
        scanned += 1
        hits = _transport_offenders(ast.parse(path.read_text()))
        if hits:
            offenders[path.name] = hits
    assert scanned >= 10, (
        f"only {scanned} module(s) scanned under {_tools_package_dir()} "
        f"— the package walk found almost nothing, so this test would "
        f"pass vacuously."
    )
    assert not offenders, (
        f"transport/stream references found in the tools package: "
        f"{offenders!r}. See "
        f"test_tool_carries_no_caller_paced_yield_primitive for why the "
        f"tool layer must stay transport-free."
    )


# ── The dispatcher's fusion property ─────────────────────────────────

_GATE_CALLS = frozenset({
    "check_capability_gate",
    "check_policy_gate",
    "check_predicate_gate",
})


def _dispatch_tool_call_ast() -> ast.AsyncFunctionDef:
    from agent_mcp.tools import registry

    tree = ast.parse(Path(registry.__file__).read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "dispatch_tool_call"
        ):
            return node
    raise AssertionError(
        "dispatch_tool_call no longer exists in tools/registry.py — it "
        "is the single seam this file's invariant is about; re-point "
        "these tests rather than deleting them.",
    )


def _gate_and_yield_linenos() -> tuple[list[int], list[int]]:
    """``(gate_call_linenos, yield_point_linenos)`` inside
    ``dispatch_tool_call``."""
    func = _dispatch_tool_call_ast()
    gates: list[int] = []
    yields: list[int] = []
    for node in ast.walk(func):
        if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
            yields.append(node.lineno)
        elif isinstance(node, ast.Call):
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else getattr(target, "id", None)
            )
            if name in _GATE_CALLS:
                gates.append(node.lineno)
    return sorted(gates), sorted(yields)


def test_dispatcher_gate_detection_is_not_vacuous() -> None:
    """The two ordering assertions below are only meaningful if the gate
    calls are actually found. R20-F4/R21-F1 wired all three
    (capability / policy / predicate) into ``dispatch_tool_call``'s
    pre-schema branch; if a refactor moves them behind a helper this
    detector must fail here, loudly, rather than let the ordering tests
    pass over an empty set."""
    gates, yields = _gate_and_yield_linenos()
    assert len(gates) == 3, (
        f"expected all three pre-schema gate calls "
        f"({sorted(_GATE_CALLS)!r}) inline in dispatch_tool_call; found "
        f"{len(gates)} at lines {gates!r}. If the gate moved behind a "
        f"helper, this file must follow it — the invariant is about "
        f"where the gate runs relative to the dispatcher's yield "
        f"points, so it cannot be checked from here any more."
    )
    assert yields, (
        "no yield point at all found in dispatch_tool_call — it awaits "
        "the tool implementation, so the AST detector has stopped "
        "matching."
    )


def test_no_yield_point_before_the_authorization_gate() -> None:
    """Nothing in ``dispatch_tool_call`` may suspend before the
    authorization gate runs.

    A yield point here would put an arbitrary, potentially caller-
    influenced delay between "the Principal was built by the transport
    middleware" and "the gate consults it" — the front half of the same
    TOCTOU the router surface fixed by making the fused helper the
    FIRST yield point in every handler (see
    ``test_no_bare_yield_point_before_revalidation``).
    """
    gates, yields = _gate_and_yield_linenos()
    first_gate = min(gates)
    early = [ln for ln in yields if ln < first_gate]
    assert not early, (
        f"dispatch_tool_call awaits at line(s) {early!r} BEFORE its "
        f"authorization gate at line {first_gate} — the Principal "
        f"resolved at request entry can be revoked in that window and "
        f"the gate would still admit on the stale snapshot. Keep every "
        f"pre-dispatch step synchronous, or fuse the new await with a "
        f"fresh re-derivation of the Principal the way "
        f"perm_gates.read_body_and_revalidate does on the router "
        f"surface."
    )


def test_no_yield_point_between_gate_and_tool_invocation() -> None:
    """The authorization gate and the tool invocation must be fused by
    the absence of any yield point between them.

    This is the property the whole tools surface rests on: the gate's
    verdict is still true when the implementation runs because nothing
    could have happened in between. It holds today by construction —
    schema cleaning, ``jsonschema.validate``, the oversized-string
    backstop and the signature probe are all synchronous — which makes
    it exactly the kind of invariant that gets broken by a
    well-intentioned one-line addition (``await audit.record(...)``,
    ``await rate_limiter.acquire(...)``). Pin it.
    """
    gates, yields = _gate_and_yield_linenos()
    last_gate = max(gates)
    impl_awaits = [ln for ln in yields if ln > last_gate]
    assert impl_awaits, (
        "no yield point found after the gate — dispatch_tool_call must "
        "still await the tool implementation; the detector has stopped "
        "matching."
    )
    # Every yield point after the gate must BE the implementation call
    # itself (dispatch_tool_call awaits it on two branches: with and
    # without ``principal=``). Anything else is an interleaving point
    # between the verdict and the act.
    func = _dispatch_tool_call_ast()
    interlopers: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
            continue
        if node.lineno <= last_gate:
            continue
        value = getattr(node, "value", None)
        target = value.func if isinstance(value, ast.Call) else None
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name != "implementation_func":
            interlopers.append(f"line {node.lineno}: await {name}")
    assert not interlopers, (
        f"dispatch_tool_call yields at {interlopers!r} between its "
        f"authorization gate (line {last_gate}) and the tool "
        f"invocation. That window lets a concurrent capability / "
        f"membership / session revocation land after the gate said yes "
        f"and before the tool writes — the OBS-R11-1 shape, on a "
        f"surface with no re-validation seam of its own. Either keep "
        f"the stretch synchronous, or add the tools-surface equivalent "
        f"of perm_gates.revalidate_after and re-run the gate once the "
        f"new await resolves."
    )
