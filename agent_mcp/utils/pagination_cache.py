# Agent-MCP/agent_mcp/utils/pagination_cache.py
"""``StableOrderCache`` — anchors a page-1 ordering so offset pagination
survives concurrent mutation of the underlying set (R17-F2).

The problem
-----------

Every offset/limit list surface in this codebase (``view_tasks``,
``list_agents``, ``/api/messages/query``) re-filters and re-sorts a
FRESH snapshot of a live, mutating source on every call, then slices
``matched[offset:offset+limit]``. That slicing is only correct if
``matched`` is identical across calls. It usually isn't: ordinary
concurrent activity (a task changes status, an agent terminates, a
message gets marked read and drops out of an ``unread`` filter)
between page N and page N+1 shifts every row ranked after the change,
so a row that was never returned on ANY page can land exactly in the
gap and get silently skipped. Live-reproduced for ``view_tasks``: 5
pending tasks, ``limit=2``; page 1 returns [T5, T4]; T5 moves to
``in_progress``; page 2 (``offset=2``) returns [T2, T1] — T3 was
pending the entire time and never appeared on either page.

Why this can't be fixed while keeping bare integer offsets
------------------------------------------------------------

Numeric offset is a POSITION, not an identity. Making it immune to
concurrent mutation of the underlying set requires one of:

1. An identity anchor from the previous page (keyset/cursor
   pagination — ``view_tasks(start_after=<task_id>)`` already does
   this and is unaffected by this bug; see its docstring for the one
   remaining edge case it doesn't cover).
2. Freezing the ordering computed for page 1 and replaying it for
   subsequent pages of the SAME pagination sweep — what this module
   does.

There is no way to keep "offset means skip N of whatever currently
matches" and also be safe under concurrent mutation; those two
requirements are contradictory. Given the existing ``offset``/``limit``
contract is public (MCP tool schema + REST API) and changing it would
be a breaking change across 3 surfaces, option 2 is the fix applied
here: it requires no API change and closes the reported skip for the
overwhelming common case (one caller stepping through pages
sequentially).

The trade-off (disclosed, deliberately narrow)
-----------------------------------------------

The cache is keyed by the query SHAPE (filters + sort), not by a
per-caller session token — there is no session concept on these MCP
tool / REST surfaces to hang a token off without a real API addition.
Concretely: if a caller starts a *new* sweep (``offset=0``) for a
filter shape that ANOTHER caller is still mid-sweep on, the second
sweep's population overwrites the first's anchor, and the first
caller's next page replays the second caller's ordering instead of
their own. This is much narrower than the bug it replaces — it only
manifests when two independent callers page the identical filter+sort
shape concurrently — but it is not airtight. A fully airtight fix
needs an explicit opaque cursor issued per pagination sweep, judged
out of scope for this fix (see the R17-F2 PR description).

If the cache has no entry for a requested offset>0 (expired, evicted,
or the caller jumped straight to a mid-sweep offset without ever
requesting offset=0), :meth:`replay_or_none` returns ``None`` and the
caller must fall back to a fresh recompute — the pre-fix behaviour,
with no anchor to be safe against.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Generic, Hashable, List, Optional, Tuple, TypeVar

_T = TypeVar("_T")


class StableOrderCache(Generic[_T]):
    """Bounded, TTL'd cache of "the ordered id sequence seen at the start
    of a pagination sweep", keyed by an arbitrary hashable shape key
    (typically the filter+sort spec).

    Not a general-purpose cache: it exists solely to give
    offset-pagination call sites a way to replay a consistent ordering
    across calls instead of recomputing (and thus potentially
    re-shifting) it fresh every time. See the module docstring for the
    full rationale and disclosed trade-off.
    """

    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 512) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._store: Dict[Hashable, Tuple[float, List[_T]]] = {}

    def replay_or_none(self, key: Hashable) -> Optional[List[_T]]:
        """Return the cached ordering for ``key`` if present and not
        expired, else ``None`` (cache miss — caller must recompute
        fresh with no consistency guarantee)."""
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, ids = entry
            if now - ts > self._ttl:
                del self._store[key]
                return None
            return ids

    def anchor(self, key: Hashable, ids: List[_T]) -> None:
        """(Re)populate the cache for ``key`` — called when a sweep
        starts (``offset == 0``), so a following ``offset > 0`` call
        for the same shape can replay this exact ordering."""
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            if key not in self._store and len(self._store) >= self._max_entries:
                oldest_key = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest_key]
            self._store[key] = (now, list(ids))

    def clear(self) -> None:
        """Drop every anchored entry.

        Callers embedding a MODULE- or CLASS-level ``StableOrderCache``
        (so it survives across the fresh per-call objects that hold it
        — see e.g. ``TaskFilterSpec`` cache-key users) must register
        this with their test-isolation seam (``tests.conftest.
        reset_and_snapshot_globals`` in this repo) — a cache anchored
        by one test's fixture data would otherwise leak stale ids into
        the next test that happens to build the identical filter/sort
        cache key.
        """
        with self._lock:
            self._store.clear()

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, (ts, _ids) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]

    def get_or_anchor(
        self,
        key: Hashable,
        *,
        offset: int,
        compute: Callable[[], List[_T]],
    ) -> List[_T]:
        """The single call sites need: ``offset == 0`` always recomputes
        fresh and (re)anchors the cache (starting/restarting a sweep);
        ``offset > 0`` replays the anchored ordering when present, else
        falls back to ``compute()``. ``compute()`` always returns the
        FULL ordering (offset is applied by the caller afterwards), so
        even a fallback call anchors it — closing the gap for any
        later call in the same sweep after just one unanchored page."""
        if offset:
            cached = self.replay_or_none(key)
            if cached is not None:
                return cached
        ids = compute()
        self.anchor(key, ids)
        return ids


__all__ = ["StableOrderCache"]
