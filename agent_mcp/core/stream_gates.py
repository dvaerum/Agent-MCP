"""Streaming-lifecycle revalidation seam (Finding N5).

``router/perm_gates.py`` fuses the REQUEST lifecycle's two-step
"do the yield point, then re-check authorization" into one call
(``read_body_and_revalidate`` / ``revalidated_lock`` /
``revalidate_after``), because seven pentest rounds proved that a
re-check you have to REMEMBER to call is a re-check that eventually
gets forgotten. This module is the same fusion for the STREAMING
lifecycle.

Why it exists
-------------
Four long-lived streams in this codebase authenticate ONCE at open and
then pump indefinitely:

======================================  ===========================  ========
stream                                  liveness predicate           cadence
======================================  ===========================  ========
``GET /api/events`` (events.py)         ``require_operator_session``  15 s
``GET /api/<p>/delivery/stream``        ``is_active_agent``           15 s
``GET /mcp`` SSE pump (main_app.py)     ``_bearer_is_active``         15 s
``wait_for_events`` long-poll           ``_check_auto_event_loop_``   2 s
                                        ``flags``
======================================  ===========================  ========

Each one independently grew the same ~15-line loop — re-check, bounded
wait, re-check again after the dequeue — under a different finding ID
(R5-F1, R13-F2, AC-R29-1/SEC-B-F2, AC-R29-1 class-sweep), tied together
only by comments in four files citing each other's finding IDs. Nothing
was broken; what was fragile is that the FIFTH stream's author would
have had to infer, from four cross-referencing comments, that those
loops describe a REQUIREMENT rather than four pieces of history. The
half that gets dropped when the pattern is copied by hand is
specifically the *second* re-check, the one after the dequeue
(SEC-B-F2 found exactly that omission on the GET /mcp pump: a payload
queued before revocation reached the wire because only the top of the
loop re-checked).

:class:`RevalidatingStream` makes that structural instead: the ONLY
await of the underlying queue anywhere in ``agent_mcp/`` lives in
:meth:`RevalidatingStream.next_slice` below, and every path out of that
method runs a fresh liveness check first. There is no way to obtain the
next event without the re-validation having run, because they are the
same call. ``tests/test_arch_enforced_stream_revalidation.py`` is the
backstop that keeps it that way: it AST-discovers every queue-dequeue
await in the package and fails on any that isn't this one.

What is fused, and what is deliberately NOT
-------------------------------------------
Fused (the STRUCTURE, identical for all four streams):

* the bounded wait, clamped so a slice can never outlast the stream's
  own revalidation cadence (the "keep the slice no longer than the
  heartbeat" half of the pattern — a caller cannot widen it, only
  shorten it, see ``timeout=`` on :meth:`next_slice`);
* the liveness re-check after a dequeue, BEFORE the item is handed
  back (SEC-B-F2's half);
* the liveness re-check after an idle expiry, BEFORE the caller runs
  its idle-branch work (heartbeat / scheduled-fire / reminder — the
  ``wait_for_events`` idle branch can return real event content, so
  this is a delivery path too);
* fail-closed teardown as an EXCEPTION (:class:`StreamRevoked`), not a
  return value a caller can forget to inspect: a stream author who
  ignores it gets teardown, never delivery.

NOT fused (deliberately — each stream keeps its own):

* **the liveness predicate.** The three predicates have genuinely
  different staleness/cost characteristics — ``_bearer_is_active`` is
  an in-memory cache read (cheap, can lag a commit),
  ``is_active_agent`` is the canonical repository predicate, and
  ``_check_auto_event_loop_flags`` is a live single-row DB read
  (costlier, most current) — and ``/api/events``' predicate is a whole
  FastAPI dependency re-run (session validity AND project membership).
  Collapsing them into one shared check would silently change what
  each stream tolerates and what it costs. The predicate is therefore
  a constructor argument, not a policy decision this module makes.
* **the cadence.** 15 s for the three SSE channels, 2 s for
  ``wait_for_events``' flag-recheck slice — also a constructor
  argument.

Why the check runs AFTER the wait rather than before it
-------------------------------------------------------
The four hand-rolled loops were written as "check, wait, (on item)
check again"; this seam is "wait, check, hand back". The two are
equivalent in every property that matters and the second is strictly
cheaper:

* nothing is ever delivered on a stale verdict — the check immediately
  precedes the hand-back on BOTH paths (item and idle), where the
  hand-rolled shape only covered the item path;
* revocation is still noticed within one cadence interval — the
  post-idle check sits at exactly the same wall-clock instant the old
  top-of-loop check would have run one statement later;
* the stream's OPEN-time gate (``Depends``/middleware/dispatcher) is
  what covers the very first wait, which is what it always did;
* the old top-of-loop check on an item iteration was redundant with
  the previous iteration's own post-check, so one check per slice
  replaces one-or-two with no loss.

Known asyncio wrinkle, now in one place: ``asyncio.wait_for`` cancels
the inner ``queue.get()`` on timeout, and a put that lands in that
cancellation window can be dropped. All four streams inherited that
from the pattern; centralising the wait means a future fix (e.g.
``asyncio.timeout`` + a persistent getter task) is a one-file change
rather than a four-file sweep.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, Union


__all__ = [
    "Liveness",
    "LivenessCheck",
    "RevalidatingStream",
    "StreamRevoked",
    "StreamSlice",
]


@dataclass(frozen=True)
class Liveness:
    """A liveness verdict: may this stream still deliver, and if not, why.

    ``reason`` is surfaced to the caller through
    :attr:`StreamRevoked.reason`; ``wait_for_events`` puts it straight
    into its ``stop_listening`` payload, the SSE streams use it for the
    teardown log line. A predicate may also return a plain ``bool``
    (see :func:`_coerce`) when it has no reason to give.
    """

    live: bool
    reason: Optional[str] = None

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.live


#: What a liveness predicate may return: a bare bool, a
#: :class:`Liveness`, or an awaitable of either (``/api/events``'
#: predicate re-runs an async FastAPI dependency).
LivenessResult = Union[bool, Liveness]
LivenessCheck = Callable[[], Union[LivenessResult, Awaitable[LivenessResult]]]


class _Dequeueable(Protocol):
    """The one thing a stream source must provide: an awaitable
    ``get()`` (``asyncio.Queue`` and anything shaped like it)."""

    def get(self) -> Awaitable[Any]:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class StreamSlice:
    """The outcome of ONE bounded wait, already re-validated.

    Exactly two shapes, and both have had a fresh liveness verdict
    taken immediately before the caller received them:

    * ``idle=True`` — the bounded wait expired with no event. The
      caller does its per-tick work (SSE heartbeat, scheduled-directive
      fire, idle reminder) and asks for the next slice.
    * ``idle=False`` — ``item`` is the dequeued event, cleared for
      delivery.

    A revoked stream is NOT a third shape: it raises
    :class:`StreamRevoked` instead, so "forgot to handle revocation"
    fails closed (the stream dies) rather than open (the caller reads
    ``item`` as ``None`` and carries on).
    """

    item: Any = None
    idle: bool = False


class StreamRevoked(Exception):
    """Raised by :meth:`RevalidatingStream.next_slice` when the stream's
    liveness predicate says the caller is no longer entitled to it.

    ``phase`` is ``"item"`` when the verdict was taken after a dequeue
    (``discarded`` then holds the event that must NOT reach the wire)
    and ``"idle"`` when it was taken after the bounded wait expired.
    Callers tear down; the only variation between the four streams is
    what they emit on the way out.
    """

    def __init__(
        self,
        verdict: Liveness,
        *,
        phase: str,
        discarded: Any = None,
    ) -> None:
        self.verdict = verdict
        self.phase = phase
        self.discarded = discarded
        super().__init__(verdict.reason or f"stream revoked ({phase})")

    @property
    def reason(self) -> Optional[str]:
        return self.verdict.reason


def _coerce(result: LivenessResult) -> Liveness:
    """Normalise a predicate's return value to a :class:`Liveness`."""
    if isinstance(result, Liveness):
        return result
    return Liveness(bool(result))


class RevalidatingStream:
    """Bounded wait for the next event, fused with a liveness re-check.

    One instance per open stream::

        gate = RevalidatingStream(
            sub.queue,
            liveness=lambda: is_active_agent(agent_id),
            interval=lambda: REVALIDATE_SECONDS,
        )
        while True:
            try:
                sl = await gate.next_slice()
            except StreamRevoked:
                return                      # teardown; finally cleans up
            if sl.idle:
                continue                    # or: heartbeat, then continue
            yield {"data": json.dumps(sl.item)}

    ``liveness`` is this stream's OWN predicate (see the module
    docstring on why it isn't unified) and may be sync or async.
    ``interval`` is this stream's OWN cadence — a float, or a callable
    re-read on every slice when the value can change at runtime (the
    SSE modules' ``REVALIDATE_SECONDS`` is monkeypatched by their
    regression tests, and the ``/mcp`` pump's heartbeat is a per-
    instance attribute).
    """

    def __init__(
        self,
        queue: _Dequeueable,
        *,
        liveness: LivenessCheck,
        interval: Union[float, Callable[[], float]],
    ) -> None:
        self._queue = queue
        self._liveness = liveness
        self._interval = interval

    def cadence(self) -> float:
        """This stream's current revalidation interval, in seconds."""
        interval = self._interval
        return float(interval() if callable(interval) else interval)

    async def check(self) -> Liveness:
        """Run this stream's liveness predicate once, sync or async."""
        result = self._liveness()
        if inspect.isawaitable(result):
            result = await result
        return _coerce(result)

    async def next_slice(
        self, *, timeout: Optional[float] = None
    ) -> StreamSlice:
        """Wait for the next event, re-validate, and hand back a slice.

        Raises :class:`StreamRevoked` — before returning anything at
        all — the moment this stream's predicate says the caller is no
        longer live. Never returns an unchecked value: both exits
        (dequeued item, idle expiry) sit immediately after their own
        fresh verdict.

        ``timeout`` lets a caller SHORTEN one slice when it has an
        earlier deadline of its own to honour (``wait_for_events``
        wakes at the soonest of its flag-recheck tick, hold deadline,
        idle-stop, scheduled fire and reminder). It can never LENGTHEN
        one: the value is clamped to this stream's cadence, so the
        revalidation interval is a property of the stream rather than
        of whatever the caller last computed.
        """
        budget = self.cadence()
        if timeout is not None:
            budget = min(budget, timeout)
        budget = max(0.0, budget)

        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=budget)
        except asyncio.TimeoutError:
            verdict = await self.check()
            if not verdict.live:
                raise StreamRevoked(verdict, phase="idle") from None
            return StreamSlice(idle=True)

        verdict = await self.check()
        if not verdict.live:
            raise StreamRevoked(verdict, phase="item", discarded=item)
        return StreamSlice(item=item)
