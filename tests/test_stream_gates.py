"""Finding N5 — the streaming-lifecycle fusion primitive itself.

``core/stream_gates.RevalidatingStream`` is to the STREAMING lifecycle
what ``router/perm_gates.read_body_and_revalidate`` is to the REQUEST
lifecycle: a caller cannot obtain the thing it wants (there, the parsed
body; here, the next event) without the authorization/liveness re-check
having ALSO run, because they are the same call.

These tests pin that fusion property directly — every exit path of
:meth:`RevalidatingStream.next_slice` runs exactly one fresh liveness
check first, on both the dequeue path and the idle path — plus the two
structural details the four hand-rolled copies each had to remember:
the post-dequeue re-check (SEC-B-F2's half) and the slice never
outlasting the stream's own cadence.

The per-stream regression suites (``test_sec_r5f1_events_revalidation``,
``test_delivery_bearer_liveness``, ``test_sec_r29_terminate_sse_revoke``,
``test_sec_b_stream_teardown_symmetry``) remain the proof that each
migrated stream still behaves exactly as before; this file is the proof
that the shared seam they now share cannot be used unsafely.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_mcp.core.stream_gates import (
    Liveness,
    RevalidatingStream,
    StreamRevoked,
)

pytestmark = pytest.mark.asyncio


class _CountingCheck:
    """Liveness predicate recording every call, with a flippable verdict."""

    def __init__(self, live: bool = True, reason: str | None = None) -> None:
        self.live = live
        self.reason = reason
        self.calls = 0

    def __call__(self) -> Liveness:
        self.calls += 1
        return Liveness(self.live, self.reason)


def _gate(queue, check, interval=0.02) -> RevalidatingStream:
    return RevalidatingStream(queue, liveness=check, interval=interval)


# ── the fusion property: no exit path skips the check ────────────────


@pytest.mark.parametrize("has_item", [True, False], ids=["item", "idle"])
@pytest.mark.parametrize("live", [True, False], ids=["live", "revoked"])
async def test_every_exit_path_runs_exactly_one_fresh_check(has_item, live):
    """All four combinations of (item waiting / nothing waiting) x
    (still live / revoked) leave the seam having run the liveness
    predicate exactly once — there is no way to get a slice, or to be
    told the stream ended, without a fresh verdict."""
    queue: asyncio.Queue = asyncio.Queue()
    if has_item:
        queue.put_nowait({"marker": "payload"})
    check = _CountingCheck(live=live)
    gate = _gate(queue, check)

    if live:
        sl = await gate.next_slice()
        assert sl.idle is (not has_item)
        if has_item:
            assert sl.item == {"marker": "payload"}
    else:
        with pytest.raises(StreamRevoked):
            await gate.next_slice()

    assert check.calls == 1, (
        f"expected exactly one liveness check per slice, got {check.calls}"
    )


async def test_item_is_never_returned_when_revoked_during_the_wait():
    """The post-dequeue half (SEC-B-F2): an event queued BEFORE
    revocation, reaching the front of the FIFO after it, must never be
    handed to the caller — the seam raises instead, carrying the
    dropped payload for the caller's teardown log."""
    queue: asyncio.Queue = asyncio.Queue()
    check = _CountingCheck(live=True)
    gate = _gate(queue, check, interval=5.0)

    task = asyncio.create_task(gate.next_slice())
    await asyncio.sleep(0.01)  # park the seam inside the bounded wait
    assert not task.done()

    check.live = False  # revoked while we were blocked in get()
    queue.put_nowait({"marker": "in-flight"})

    with pytest.raises(StreamRevoked) as excinfo:
        await asyncio.wait_for(task, timeout=2.0)
    assert excinfo.value.phase == "item"
    assert excinfo.value.discarded == {"marker": "in-flight"}


async def test_idle_expiry_is_also_revalidated_before_the_caller_runs():
    """The idle path is a delivery path too (``wait_for_events``' idle
    branch can return scheduled-fire / reminder content), so the seam
    re-checks before handing back an idle slice, not only before an
    item."""
    queue: asyncio.Queue = asyncio.Queue()
    check = _CountingCheck(live=False, reason="agent 'w1' terminated")
    gate = _gate(queue, check, interval=0.01)

    with pytest.raises(StreamRevoked) as excinfo:
        await gate.next_slice()
    assert excinfo.value.phase == "idle"
    assert excinfo.value.reason == "agent 'w1' terminated"
    assert excinfo.value.discarded is None


async def test_revocation_raises_rather_than_returning_a_flag():
    """Fail-closed ergonomics: teardown is an exception, so a stream
    author who forgets to handle it loses the stream (safe) instead of
    reading ``item is None`` and carrying on (unsafe)."""
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("payload")
    gate = _gate(queue, _CountingCheck(live=False, reason="logged out"))

    with pytest.raises(StreamRevoked) as excinfo:
        await gate.next_slice()
    assert "logged out" in str(excinfo.value)


# ── the cadence bound ────────────────────────────────────────────────


async def test_slice_never_outlasts_the_streams_cadence():
    """A caller may SHORTEN one slice but never LENGTHEN it: the
    revalidation interval belongs to the stream, not to whatever the
    caller last computed."""
    queue: asyncio.Queue = asyncio.Queue()
    check = _CountingCheck(live=True)
    gate = _gate(queue, check, interval=0.02)

    loop = asyncio.get_event_loop()
    started = loop.time()
    sl = await gate.next_slice(timeout=30.0)  # would hang if honoured
    assert sl.idle is True
    assert loop.time() - started < 5.0


async def test_caller_supplied_timeout_can_shorten_a_slice():
    queue: asyncio.Queue = asyncio.Queue()
    gate = _gate(queue, _CountingCheck(live=True), interval=30.0)

    loop = asyncio.get_event_loop()
    started = loop.time()
    sl = await gate.next_slice(timeout=0.01)
    assert sl.idle is True
    assert loop.time() - started < 5.0


async def test_negative_timeout_is_clamped_to_zero():
    """An already-past deadline degenerates to "poll once", never to a
    negative wait_for."""
    queue: asyncio.Queue = asyncio.Queue()
    gate = _gate(queue, _CountingCheck(live=True), interval=30.0)

    sl = await gate.next_slice(timeout=-1.0)
    assert sl.idle is True


async def test_cadence_is_re_read_each_slice_when_callable():
    """A callable ``interval`` is re-read on every slice, which is what
    lets the SSE modules keep their monkeypatchable module-level
    ``REVALIDATE_SECONDS`` and the ``/mcp`` pump its per-instance
    heartbeat attribute."""
    queue: asyncio.Queue = asyncio.Queue()
    values = [30.0, 0.01]
    gate = RevalidatingStream(
        queue,
        liveness=_CountingCheck(live=True),
        interval=lambda: values[-1],
    )
    assert gate.cadence() == 0.01
    values.append(0.02)
    assert gate.cadence() == 0.02


# ── predicate shapes ─────────────────────────────────────────────────


async def test_async_predicate_is_awaited():
    """``/api/events``' predicate re-runs an async FastAPI dependency."""
    queue: asyncio.Queue = asyncio.Queue()
    calls = []

    async def _check() -> bool:
        calls.append(1)
        return False

    gate = RevalidatingStream(queue, liveness=_check, interval=0.01)
    with pytest.raises(StreamRevoked):
        await gate.next_slice()
    assert calls == [1]


async def test_bare_bool_predicate_is_accepted():
    """``is_active_agent`` / ``_bearer_is_active`` return plain bools."""
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("x")
    gate = RevalidatingStream(queue, liveness=lambda: True, interval=0.01)
    assert (await gate.next_slice()).item == "x"


async def test_reason_tuple_predicate_shape():
    """``wait_for_events``' predicate returns ``(enabled, reason)``; the
    seam's ``Liveness`` maps onto it positionally, so no adapter code is
    needed at the call site."""
    queue: asyncio.Queue = asyncio.Queue()

    def _flags() -> tuple[bool, str | None]:
        return False, "config_auto_event_loop_global is OFF"

    gate = RevalidatingStream(
        queue, liveness=lambda: Liveness(*_flags()), interval=0.01
    )
    with pytest.raises(StreamRevoked) as excinfo:
        await gate.next_slice()
    assert excinfo.value.reason == "config_auto_event_loop_global is OFF"
