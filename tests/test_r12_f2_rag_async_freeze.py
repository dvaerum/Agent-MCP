"""R12-F2 (HIGH, CONFIRMED live-exploited DoS): ``query_rag_system`` /
``query_rag_system_with_model`` were ``async def`` but resolved their
vector-search embedding via the embedding client's SYNCHRONOUS
``.embed()`` instead of its async twin ``.aembed()`` — with no
``run_in_executor``/thread offload. ``_BaseEmbeddingClient.embed()``
is a genuinely blocking network round-trip (a plain ``openai.OpenAI``
client under the hood). Called synchronously from inside a coroutine
that has NO earlier ``await`` (both call sites embed the query before
the function's first ``await``), the blocking call CANNOT yield — it
holds the single asyncio event-loop thread hostage for its whole
duration, freezing the ENTIRE per-project backend: every other REST
endpoint, every other agent's stream, every other task/message op.

Live-confirmed: a single ``ask_project_rag`` call (reachable by ANY
worker-role agent via the base ``rag.query`` capability) froze the
target backend for 15+ minutes; guest CPU was idle (0.09 load),
confirming a genuine event-loop stall rather than slow inference;
recovery required a manual ``systemctl restart``.

This test reproduces the freeze PROPERTY deterministically — no
reliance on a real 15-minute timing window or a real network call.
The embedding stand-in's ``.embed()`` blocks the calling THREAD for a
short, fixed duration (mirroring the real blocking httpx round-trip);
its ``.aembed()`` instead ``await``s, mirroring genuine async I/O. A
lightweight "ticker" coroutine is scheduled on the SAME event loop
before the RAG query starts and should keep making progress
throughout the query — UNLESS the query's embedding call is the
synchronous, blocking one, in which case the ticker cannot run at all
until the blocking call returns (there is no earlier ``await`` in
either query function for the scheduler to preempt on).

RED on the pre-fix code (call sites used ``.embed()``): the ticker
gets essentially zero ticks while the query is in flight, because the
whole coroutine — including the blocking embed() call — runs to its
first ``await`` with the event loop unable to schedule anything else.

GREEN after the fix (call sites use ``await ...aembed()``): the mock's
``aembed()`` ``await``s instead of blocking the thread, so the ticker
keeps ticking throughout — proving the fix actually restores event-loop
concurrency, not just that the code compiles.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_mcp.features.rag import query as query_mod
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio

# Long enough that a frozen loop produces an unambiguous zero-ish tick
# count, short enough the test stays fast.
_BLOCK_SECONDS = 0.5
_TICK_INTERVAL = 0.01
# Over _BLOCK_SECONDS at _TICK_INTERVAL we'd expect ~50 ticks if the
# loop is never blocked. A frozen loop produces ~0-1 (only once the
# blocking call itself returns and the coroutine finally reaches an
# `await`). Comfortably below the unblocked count, comfortably above
# what a frozen loop can produce.
_MIN_TICKS_WHEN_NOT_FROZEN = 15


class _Chat:
    """Minimal completion_client() stand-in: answers instantly so the
    only thing this test is timing is the embedding call."""

    provider = "mock"
    model = "mock"

    async def chat(self, messages, temperature: float = 0.4) -> str:
        return "SYNTHESISED-ANSWER"


class _BlockingEmbedder:
    """Stands in for ``embedding_client()``.

    ``.embed()`` genuinely blocks the calling THREAD via ``time.sleep``
    — mirroring ``_BaseEmbeddingClient.embed()``'s blocking httpx round
    trip. ``.aembed()`` instead ``await``s via ``asyncio.sleep`` —
    mirroring genuine async I/O. Only a caller that (bug) reaches for
    the synchronous method from a coroutine can freeze the loop; a
    caller that correctly ``await``s ``aembed()`` cannot.
    """

    def __init__(self, dimension: int = 8) -> None:
        self._dimension = dimension

    def embed(self, texts):
        time.sleep(_BLOCK_SECONDS)
        return [[0.0] * self._dimension for _ in texts]

    async def aembed(self, texts):
        await asyncio.sleep(_BLOCK_SECONDS)
        return [[0.0] * self._dimension for _ in texts]


async def _ticker(stop: asyncio.Event, ticks: list) -> None:
    """Trivial concurrent coroutine on the SAME event loop. Its tick
    count during the RAG query is the freeze/no-freeze signal."""
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(_TICK_INTERVAL)


async def _run_query_with_concurrent_ticker(
    monkeypatch, tmp_path, *, query_kwargs: dict | None = None
) -> list:
    async with mcp_session(tmp_path):
        from agent_mcp.repositories import get_rag_repo

        monkeypatch.setattr(query_mod, "embedding_client", lambda: _BlockingEmbedder())
        monkeypatch.setattr(query_mod, "completion_client", lambda: _Chat())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: True)
        monkeypatch.setattr(get_rag_repo(), "search_similar", lambda **kw: [])

        stop = asyncio.Event()
        ticks: list[float] = []
        ticker_task = asyncio.create_task(_ticker(stop, ticks))
        # Give the scheduler an explicit chance to start the ticker
        # before the query call so a non-frozen loop is guaranteed at
        # least one tick to interleave against.
        await asyncio.sleep(0)

        await query_mod.query_rag_system("does this freeze the loop?", **(query_kwargs or {}))

        stop.set()
        await ticker_task
        return ticks


async def test_rag_query_embedding_does_not_freeze_event_loop(
    tmp_path, monkeypatch
) -> None:
    """query_rag_system's embedding call must not starve a concurrent
    coroutine on the same event loop (R12-F2)."""
    ticks = await _run_query_with_concurrent_ticker(monkeypatch, tmp_path)

    assert len(ticks) >= _MIN_TICKS_WHEN_NOT_FROZEN, (
        "a concurrent coroutine on the same event loop barely ran while "
        "query_rag_system's embedding call was in flight -- the event "
        f"loop was frozen for ~{_BLOCK_SECONDS}s. R12-F2: query_rag_system "
        "must call the embedding client's ASYNC aembed(), not the "
        f"blocking sync embed(). (observed {len(ticks)} ticks; expected "
        f">= {_MIN_TICKS_WHEN_NOT_FROZEN} over ~{_BLOCK_SECONDS}s at "
        f"{_TICK_INTERVAL}s intervals)"
    )


async def test_rag_query_with_model_embedding_does_not_freeze_event_loop(
    tmp_path, monkeypatch
) -> None:
    """query_rag_system_with_model (the create_self_task placement-
    validator's entry point) has the identical bug at its own vector-
    search call site (R12-F2 class-sweep sibling)."""
    async with mcp_session(tmp_path):
        from agent_mcp.repositories import get_rag_repo

        monkeypatch.setattr(query_mod, "embedding_client", lambda: _BlockingEmbedder())
        monkeypatch.setattr(query_mod, "completion_client", lambda: _Chat())
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: True)
        monkeypatch.setattr(get_rag_repo(), "search_similar", lambda **kw: [])

        stop = asyncio.Event()
        ticks: list[float] = []
        ticker_task = asyncio.create_task(_ticker(stop, ticks))
        await asyncio.sleep(0)

        await query_mod.query_rag_system_with_model(
            "does this freeze the loop too?"
        )

        stop.set()
        await ticker_task

    assert len(ticks) >= _MIN_TICKS_WHEN_NOT_FROZEN, (
        "a concurrent coroutine on the same event loop barely ran while "
        "query_rag_system_with_model's embedding call was in flight -- "
        f"the event loop was frozen for ~{_BLOCK_SECONDS}s. R12-F2: "
        "query_rag_system_with_model must call the embedding client's "
        f"ASYNC aembed(), not the blocking sync embed(). (observed "
        f"{len(ticks)} ticks; expected >= {_MIN_TICKS_WHEN_NOT_FROZEN})"
    )


async def test_index_task_data_embedding_does_not_freeze_event_loop(
    tmp_path, monkeypatch
) -> None:
    """index_task_data (fired via asyncio.create_task() on EVERY
    ordinary create_task/update_task call) has the identical bug at its
    per-chunk embedding call site (R12-F2 class-sweep sibling) — this
    one degrades the backend during routine, non-adversarial use, not
    just deliberate abuse."""
    from agent_mcp.core.config import embedding_settings
    from agent_mcp.features.rag import indexing as indexing_mod

    async with mcp_session(tmp_path):
        # Match the real rag_embeddings vec0 column's dimension so the
        # chunk insert below succeeds cleanly (this test is about the
        # event-loop-freeze property, not about exercising a dimension
        # mismatch).
        dimension = embedding_settings().dimension
        monkeypatch.setattr(
            indexing_mod,
            "embedding_client",
            lambda: _BlockingEmbedder(dimension),
        )
        monkeypatch.setattr(indexing_mod, "is_vss_loadable", lambda: True)

        stop = asyncio.Event()
        ticks: list[float] = []
        ticker_task = asyncio.create_task(_ticker(stop, ticks))
        await asyncio.sleep(0)

        await indexing_mod.index_task_data(
            "task_r12_f2_probe",
            {
                "task_id": "task_r12_f2_probe",
                "title": "probe task",
                "description": "d",
                "status": "pending",
                "assigned_to": None,
                "created_by": "admin",
                "parent_task": None,
                "depends_on_tasks": [],
                "priority": "medium",
            },
        )

        stop.set()
        await ticker_task

    assert len(ticks) >= _MIN_TICKS_WHEN_NOT_FROZEN, (
        "a concurrent coroutine on the same event loop barely ran while "
        "index_task_data's per-chunk embedding call was in flight -- the "
        f"event loop was frozen for ~{_BLOCK_SECONDS}s. R12-F2: "
        "index_task_data must call the embedding client's ASYNC "
        f"aembed(), not the blocking sync embed(). (observed "
        f"{len(ticks)} ticks; expected >= {_MIN_TICKS_WHEN_NOT_FROZEN})"
    )
