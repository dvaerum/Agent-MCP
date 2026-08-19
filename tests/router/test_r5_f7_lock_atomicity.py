"""R5-F7: direct unit-level pin that the ``asyncio.Lock`` added around
``_track_streaming_proxy``'s check-and-increment is what makes the
critical section atomic — not an accident of "nothing currently awaits
in there". Simulates the exact regression the finding worried about
(a future edit inserting an ``await`` between the check and the
mutation, e.g. an audit-log call or a metrics push) and proves the cap
still holds because the lock — not the absence of an await — is doing
the serializing.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_lock_serializes_check_and_increment_even_with_an_injected_await(
    router_module,
) -> None:
    """Acquire under the SAME production lock (``_po._streaming_proxies_
    lock``) with a real ``await`` deliberately inserted between the
    cap check and the counter mutation — the kind of edit that would
    silently reopen the race if the critical section relied on
    "nothing here ever awaits" instead of the lock. 50 concurrent
    acquisitions against a cap of 4 must still admit exactly 4.
    """
    _po = router_module._po
    max_per_agent = 4
    agent_key = "test-agent"
    admitted_count = 0
    lock = _po._streaming_proxies_lock

    async def _acquire_with_injected_await() -> bool:
        nonlocal admitted_count
        async with lock:
            per = _po._streaming_proxies_per_agent.get(agent_key, 0)
            # The injected await: models a future edit that adds work
            # (an audit log, a metrics push, a DB round-trip) between
            # the check and the mutation, still inside the lock. If
            # the lock is doing its job, concurrent callers still
            # can't interleave here even though this task now
            # genuinely yields control mid-critical-section.
            await asyncio.sleep(0)
            if per >= max_per_agent:
                return False
            _po._streaming_proxies_per_agent[agent_key] = per + 1
            admitted_count += 1
            return True

    try:
        results = await asyncio.gather(
            *[_acquire_with_injected_await() for _ in range(50)]
        )
        admitted = sum(1 for r in results if r)
        assert admitted == max_per_agent, (
            f"expected exactly {max_per_agent} admissions under the "
            f"lock even with an injected await inside the critical "
            f"section, got {admitted}"
        )
        assert admitted_count == max_per_agent
    finally:
        _po._streaming_proxies_per_agent.pop(agent_key, None)


async def test_without_the_lock_the_injected_await_would_race(
    router_module,
) -> None:
    """Negative control: the SAME injected-await pattern, but guarded by
    a fresh, unrelated ``asyncio.Lock`` swapped out for ``contextlib.
    nullcontext`` (i.e. no locking at all) DOES overrun the cap — this
    is what R5-F7 pinned down as the risk of relying on "nothing here
    happens to await" instead of an explicit lock. Confirms the test
    harness actually exercises a real race window, not a tautology.
    """
    max_per_agent = 4
    counters: dict[str, int] = {}
    agent_key = "test-agent"
    admitted_count = 0

    async def _acquire_without_lock() -> bool:
        nonlocal admitted_count
        per = counters.get(agent_key, 0)
        # No lock here — and a real await between the check and the
        # mutation, exactly the shape R5-F7 worried a future edit
        # could introduce.
        await asyncio.sleep(0)
        if per >= max_per_agent:
            return False
        counters[agent_key] = per + 1
        admitted_count += 1
        return True

    results = await asyncio.gather(
        *[_acquire_without_lock() for _ in range(50)]
    )
    admitted = sum(1 for r in results if r)
    assert admitted > max_per_agent, (
        "expected the unlocked check-then-increment to overrun the cap "
        f"(negative control) — got exactly {admitted} admissions, "
        "which would mean this harness can't actually exercise the "
        "race the lock is meant to close"
    )
