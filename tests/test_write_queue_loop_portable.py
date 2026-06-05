"""Loop-portability contract for `execute_db_write` (PR-W1a).

The global write queue (`agent_mcp.db.write_queue._global_write_queue`)
historically bound its `asyncio.Queue` and worker task to whichever
event loop happened to call `start()` first. Once that loop finished
(`asyncio.run` returned), every subsequent `execute_db_write` from a
new loop would hang forever: the `asyncio.Queue` belongs to the dead
loop, and the worker awaiting `queue.get()` is on a dead loop too.

That deadlock is what every `_inline_write_queue` monkeypatch in
the suite works around. The contract below pins the fix: the queue
must lazily rebind to the current running loop on every
`execute_db_write` call, so tests can drive writes from independent
`asyncio.run(...)` blocks without a shim.
"""

from __future__ import annotations

import asyncio

import pytest


pytest_plugins: tuple[str, ...] = ()


def _do_one_write_via_asyncio_run() -> str:
    """Drive `execute_db_write` from a fresh `asyncio.run` block.

    The operation is trivial (returns a sentinel) — what we're proving
    is that the call returns at all. Without the lazy loop-rebind, the
    second invocation of this helper inside a single pytest test would
    deadlock awaiting a future whose worker is on a dead loop.
    """
    from agent_mcp.db.connection import execute_db_write

    async def _op() -> str:
        return "ok"

    async def _driver() -> str:
        return await asyncio.wait_for(execute_db_write(_op), timeout=2.0)

    return asyncio.run(_driver())


def test_execute_db_write_from_first_asyncio_run_block(reset_globals: None) -> None:
    """Test A: single fresh-loop call must complete within 2s."""
    assert _do_one_write_via_asyncio_run() == "ok"


def test_execute_db_write_across_two_asyncio_run_blocks(reset_globals: None) -> None:
    """Test B: two back-to-back `asyncio.run` blocks in the same
    pytest test must both complete. This is the case that deadlocks
    on main today — the second call sees a queue whose worker task
    is on the dead loop from block #1."""
    assert _do_one_write_via_asyncio_run() == "ok"
    # Second loop. Without the fix, this hangs until pytest-timeout
    # (or asyncio.wait_for inside the driver) fires.
    assert _do_one_write_via_asyncio_run() == "ok"


@pytest.mark.asyncio
async def test_execute_db_write_after_prior_asyncio_run_block(
    reset_globals: None,
) -> None:
    """Test C: same pattern as B, but the second 'loop' is the
    pytest-asyncio managed loop (asyncio_mode="strict" — fixture
    style). The queue must still be usable from this loop after a
    prior `asyncio.run` block has poisoned the singleton.

    Drives the prior `asyncio.run` in a worker thread because
    `asyncio.run()` refuses to run nested inside the pytest-asyncio
    loop on the current thread.
    """
    import concurrent.futures

    # First, exhaust one fresh loop in a worker thread — that closes
    # its loop on exit, leaving the singleton's worker_task bound to a
    # dead loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        prior = await asyncio.get_running_loop().run_in_executor(
            pool, _do_one_write_via_asyncio_run
        )
    assert prior == "ok"

    # Now use the pytest-asyncio loop. The singleton must lazily
    # rebind, not crash with "got Future <X> attached to a different
    # loop" and not hang.
    from agent_mcp.db.connection import execute_db_write

    async def _op() -> str:
        return "ok-async"

    result = await asyncio.wait_for(execute_db_write(_op), timeout=2.0)
    assert result == "ok-async"
