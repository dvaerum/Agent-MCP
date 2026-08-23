"""Finding N5 architecture invariant: no long-lived stream waits for its
next event outside the revalidation seam.

Four long-lived streams — ``GET /api/events``,
``GET /api/<project>/delivery/stream``, the ``GET /mcp`` SSE pump, and
the ``wait_for_events`` long-poll — each authenticate ONCE at open and
then pump indefinitely. Each independently grew the same loop shape
(re-check, bounded wait, re-check again after the dequeue) under a
different finding ID: R5-F1, R13-F2, AC-R29-1 / SEC-B-F2, and the
AC-R29-1 class-sweep respectively. All four were correct; what was
fragile is that the FIFTH stream inherits none of it — its author would
have to infer, from four comments in four files citing each other's
finding IDs, that those loops describe a requirement rather than
history. SEC-B-F2 is the concrete evidence of the failure mode: the
half of the pattern that gets dropped when it is copied by hand is the
SECOND re-check, the one after the dequeue.

``agent_mcp/core/stream_gates.RevalidatingStream`` fuses the wait with
the re-check (the streaming-lifecycle analogue of
``router/perm_gates.read_body_and_revalidate``'s request-lifecycle
fusion). This file is the backstop that keeps the fusion universal:
it AST-discovers every "wait for the next event off a queue" in the
whole ``agent_mcp`` package and fails on any that isn't the one inside
the seam.

Detector shape
--------------
A long-lived stream's defining move is ``await <queue>.get()`` —
directly, or wrapped in ``asyncio.wait_for(...)`` for a bounded slice.
A zero-argument ``.get()`` that is awaited is unambiguous: ``dict.get``
always takes at least one argument, and no other awaited zero-arg
``get()`` exists in this codebase. So the rule is mechanical rather
than a hand-maintained list of "modules that stream":

    every awaited zero-arg ``.get()`` in ``agent_mcp/**.py`` must live
    in ``agent_mcp/core/stream_gates.py``

``test_detector_flags_a_hand_rolled_fifth_stream`` below runs the same
detector over a synthetic fifth stream (the exact code someone would
write by copying one of the four) and asserts it IS flagged — the RED
half of this test's own red/green, kept permanently rather than
verified once by hand, so the detector can never silently degrade into
something that flags nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The single module allowed to await a queue for the next event.
_SEAM_MODULE = "agent_mcp/core/stream_gates.py"

#: The four streams this finding consolidated. Used ONLY by
#: ``test_known_streams_route_through_the_seam`` as a
#: coverage-regression guard on the detector itself (mirroring
#: ``test_arch_enforced_revalidation.test_dynamic_discovery_is_superset_of_history``):
#: the rule above is what actually enforces the invariant, but if a
#: future refactor rewrote one of these four to wait on something the
#: detector doesn't recognise, the rule would go quiet while the stream
#: went unprotected. Requiring these four to keep importing the seam
#: makes that failure loud.
_KNOWN_STREAM_MODULES = [
    "agent_mcp/app/routers/events.py",
    "agent_mcp/app/routers/delivery.py",
    "agent_mcp/app/main_app.py",
    "agent_mcp/tools/agent_communication_tools.py",
]


def _package_dir() -> Path:
    """Filesystem root of the ``agent_mcp`` package, resolved via the
    package's own ``__file__`` so discovery works regardless of where
    pytest is invoked from."""
    import agent_mcp

    return Path(agent_mcp.__file__).parent


def _is_awaited_queue_get(node: ast.AST) -> bool:
    """True for ``await <expr>.get()`` — the queue-dequeue shape.

    Zero-arg only: ``dict.get(key)`` / ``mapping.get(k, default)`` take
    arguments and are not awaited; an awaited zero-arg ``.get()`` is an
    ``asyncio.Queue`` (or a work-alike) handing over the next event.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and not node.args
        and not node.keywords
    )


def _queue_waits(tree: ast.AST) -> list[int]:
    """Line numbers of every awaited queue-dequeue in ``tree``.

    Covers both the bare ``await q.get()`` and the bounded
    ``await asyncio.wait_for(q.get(), timeout=...)`` — the latter by
    walking the whole awaited expression rather than matching a fixed
    call shape, so ``anyio.fail_after``-style wrappers or a
    ``asyncio.timeout`` rewrite are caught the same way.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        for inner in ast.walk(node.value):
            if _is_awaited_queue_get(inner):
                hits.append(node.lineno)
                break
    return hits


def _source_files() -> list[Path]:
    return sorted(_package_dir().rglob("*.py"))


def _rel(path: Path) -> str:
    return str(path.relative_to(_package_dir().parent))


def _offending_files() -> dict[str, list[int]]:
    """``{relative_path: [line, ...]}`` for every queue-wait found
    outside the seam module."""
    offenders: dict[str, list[int]] = {}
    for path in _source_files():
        rel = _rel(path)
        if rel == _SEAM_MODULE:
            continue
        hits = _queue_waits(ast.parse(path.read_text()))
        if hits:
            offenders[rel] = hits
    return offenders


# ── the invariant ────────────────────────────────────────────────────


def test_no_stream_waits_for_events_outside_the_seam() -> None:
    """Every long-lived stream must take its next event from
    ``RevalidatingStream.next_slice`` — which cannot hand one over
    without a fresh liveness verdict — instead of awaiting the queue
    itself.

    A new stream that copies one of the four historical loops (or
    writes a fresh one) trips this immediately, with the seam named in
    the failure message, rather than shipping a fifth
    authenticate-once-then-pump-forever channel whose re-validation
    depends on its author having read four comments in four other
    files.
    """
    offenders = _offending_files()
    assert not offenders, (
        "these files await a queue for the next event outside the "
        f"revalidation seam ({_SEAM_MODULE}):\n"
        + "\n".join(
            f"  {path}:{','.join(str(n) for n in lines)}"
            for path, lines in sorted(offenders.items())
        )
        + "\n\nA long-lived stream must drive its loop through "
        "agent_mcp.core.stream_gates.RevalidatingStream:\n"
        "    gate = RevalidatingStream(queue, liveness=<this stream's "
        "own predicate>, interval=<this stream's own cadence>)\n"
        "    while True:\n"
        "        try:\n"
        "            sl = await gate.next_slice()\n"
        "        except StreamRevoked:\n"
        "            return\n"
        "        if sl.idle:\n"
        "            continue\n"
        "        ...deliver sl.item...\n"
        "so the post-dequeue re-check (SEC-B-F2's half of the pattern, "
        "the one that gets dropped when the loop is hand-copied) comes "
        "along for free."
    )


def test_the_seam_itself_still_owns_exactly_one_queue_wait() -> None:
    """The other side of the same coin: the seam must actually contain
    the wait it centralises. A refactor that moved the ``queue.get()``
    out of ``stream_gates.py`` while leaving the exemption in place
    would make the rule above vacuous."""
    seam = _package_dir().parent / _SEAM_MODULE
    hits = _queue_waits(ast.parse(seam.read_text()))
    assert len(hits) == 1, (
        f"{_SEAM_MODULE} should await the queue exactly once (in "
        f"RevalidatingStream.next_slice); found {len(hits)} at "
        f"lines {hits}"
    )


@pytest.mark.parametrize("module_path", _KNOWN_STREAM_MODULES)
def test_known_streams_route_through_the_seam(module_path: str) -> None:
    """Coverage-regression guard on the detector: the four streams this
    finding consolidated must keep importing the seam.

    Without this, a rewrite that replaced one stream's
    ``await queue.get()`` with some other wait primitive would silence
    the rule above (nothing left to detect) while quietly removing that
    stream's re-validation.
    """
    path = _package_dir().parent / module_path
    tree = ast.parse(path.read_text())
    imports_seam = any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.endswith("stream_gates")
        and any(
            alias.name == "RevalidatingStream" for alias in node.names
        )
        for node in ast.walk(tree)
    )
    assert imports_seam, (
        f"{module_path} is one of the four long-lived streams Finding "
        "N5 consolidated, but it no longer imports "
        "RevalidatingStream from agent_mcp.core.stream_gates — either "
        "the stream was removed (drop it from _KNOWN_STREAM_MODULES in "
        "this file, deliberately) or its re-validation regressed."
    )


# ── the detector's own RED half ──────────────────────────────────────


#: Exactly what a fifth stream's author would write today by copying
#: ``delivery.py``'s loop and forgetting the post-dequeue re-check —
#: the SEC-B-F2 shape.
_HAND_ROLLED_FIFTH_STREAM = '''
import asyncio

REVALIDATE_SECONDS = 15


async def notifications_stream(agent_id, queue):
    while True:
        if not is_active_agent(agent_id):
            return
        try:
            frame = await asyncio.wait_for(
                queue.get(), timeout=REVALIDATE_SECONDS
            )
        except asyncio.TimeoutError:
            continue
        yield {"data": frame}
'''

#: The bare-await variant (no cadence bound at all).
_UNBOUNDED_FIFTH_STREAM = '''
async def notifications_stream(queue):
    while True:
        frame = await queue.get()
        yield {"data": frame}
'''


@pytest.mark.parametrize(
    "source",
    [_HAND_ROLLED_FIFTH_STREAM, _UNBOUNDED_FIFTH_STREAM],
    ids=["bounded-hand-rolled", "unbounded"],
)
def test_detector_flags_a_hand_rolled_fifth_stream(source: str) -> None:
    """RED-half self-check, kept permanently: run the detector over a
    synthetic fifth stream and assert it IS flagged.

    This is what makes the passing state above meaningful — the rule
    passes today because all four streams were migrated, not because
    the detector stopped detecting.
    """
    assert _queue_waits(ast.parse(source)), (
        "the detector no longer flags a hand-rolled stream loop — it "
        "would pass a fifth stream straight through"
    )


def test_detector_ignores_ordinary_mapping_get() -> None:
    """Negative control: ``dict.get(...)`` (with arguments), and a
    zero-arg ``.get()`` that is never awaited, are not stream waits."""
    source = '''
async def handler(request, cache):
    body = await request.json()
    value = body.get("status")
    other = cache.get()
    return value, other
'''
    assert _queue_waits(ast.parse(source)) == []
