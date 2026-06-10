"""Regression guards for the usePagedQuery<T>() hook + dashboard migration.

PR 5 of the 2026-06-09 architecture review series. Candidate:
``usePagedQuery<T>`` — single ownership of the paginated-fetch state
machine (``{data, total, loading, error, refresh, lastFetch}``) that
the three dashboards each used to hand-roll on top of three
syntactically-different fetch backends.

Pre-migration state-of-the-fork (post PR-4 ``useFilters``):

- ``messages-dashboard.tsx`` — calls ``POST /api/messages/query`` via
  a bespoke ``callMessages`` helper, builds the body inline
  (``{token, limit, offset, ...filters}``), threads four useState
  vars (``messages``, ``loading``, ``error``, ``total``), wires a
  refresh effect on ``[filters, currentOffset]``.

- ``tasks-dashboard.tsx`` — wraps ``apiClient.getTasks()`` (GET
  ``/tasks``, no body) in a private ``useTasksData`` hook that owns
  the same 4-tuple plus a 30s cache, a 60s background refresh
  interval, and a connection guard.

- ``agents-dashboard.tsx`` — does NOT have its own fetch. Reads
  agents out of the global ``useDataStore`` (zustand) which fetches
  ALL dashboard data in one ``/api/all-data`` round-trip and
  multiplexes it across the dashboards. The hook is the wrong
  abstraction for a global-store consumer — see the architecture
  notes at the top of ``hooks/use-paged-query.ts`` for why
  agents-dashboard.tsx is intentionally out of scope for this PR.

The hook's contract:

    {
      data: T[];
      total: number;
      loading: boolean;
      error: Error | null;
      refresh: () => void;
      lastFetch: number | null;
    }

with options:

    {
      endpoint?: string;                    // default POST target
      fetchFn?: (signal) => Promise<{...}>; // escape hatch for non-POST
      filters?: object;                     // spread into POST body
      limit?: number;
      offset?: number;
      token?: string | (() => Promise<string>);
      cacheMs?: number;                     // 0 = no caching
      deps?: ReadonlyArray<unknown>;        // extra effect deps
    }

These tests are text-parse regression guards (same convention as
``test_dashboard_use_filters_hook.py``); the fork has no jsdom
infrastructure, so behaviour is verified by ``npm run build`` +
manual click-through in the live dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The hook itself --------------------------------------


def test_use_paged_query_hook_file_exists() -> None:
    """``hooks/use-paged-query.ts`` must exist as the home of the hook."""
    path = DASHBOARD / "hooks" / "use-paged-query.ts"
    assert path.is_file(), f"expected hook at {path}"


def test_use_paged_query_hook_exports_generic_function() -> None:
    """The hook must be a generic exported function ``usePagedQuery<T>``."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    assert re.search(r"export\s+function\s+usePagedQuery\s*<", src), (
        "expected `export function usePagedQuery<T>(...)` in "
        "hooks/use-paged-query.ts"
    )
    # Must use the same React primitives the other two hooks rely on.
    assert "useState" in src, (
        "expected the hook to use React's useState for the data/loading "
        "state machine"
    )
    assert "useEffect" in src, (
        "expected the hook to use React's useEffect to fire fetches"
    )
    assert "useCallback" in src, (
        "expected useCallback for stable `refresh` identity"
    )


def test_use_paged_query_hook_returns_canonical_shape() -> None:
    """The hook's return value must expose the canonical 6-field shape."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    # The six load-bearing return-surface names. We grep for each as a
    # property-style identifier so the test passes whether the return
    # is built with shorthand (``{ data, total, ... }``) or explicit
    # keys (``{ data: …, total: … }``).
    for member in ("data", "total", "loading", "error", "refresh", "lastFetch"):
        assert member in src, (
            f"expected hook to expose `{member}` on its return value"
        )


def test_use_paged_query_hook_uses_abort_controller() -> None:
    """A slow stale request must not be allowed to overwrite a fresh
    fast one. The hook must use AbortController to cancel in-flight
    fetches when filters / offset / endpoint change."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    assert "AbortController" in src, (
        "expected hook to use AbortController to cancel stale in-flight "
        "fetches when inputs change (otherwise a slow request can "
        "overwrite a fresh fast one)"
    )
    # The signal must actually be threaded into fetch().
    assert "signal" in src, (
        "expected the AbortController.signal to be threaded into fetch()"
    )


def test_use_paged_query_hook_does_post_with_token_and_pagination() -> None:
    """The default fetch path must POST a JSON body containing
    ``token`` / ``limit`` / ``offset`` and the spread filters — this
    is the ``/api/messages/query`` contract messages-dashboard relies
    on, and the contract any future paginated-query endpoint should
    follow."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    # Method MUST be POST — browsers strip bodies from GET (the bug
    # that birthed /api/messages/query in the first place).
    assert re.search(r'method\s*:\s*[\'"]POST[\'"]', src), (
        "expected the default fetch path to use method: 'POST'"
    )
    # Body must include token / limit / offset.
    for key in ("token", "limit", "offset"):
        assert key in src, (
            f"expected the POST body construction to reference `{key}`"
        )
    # JSON body — Content-Type header + JSON.stringify.
    assert "application/json" in src, (
        "expected the POST to set Content-Type: application/json"
    )
    assert "JSON.stringify" in src, (
        "expected the POST body to be JSON.stringify'd"
    )


def test_use_paged_query_hook_accepts_fetch_fn_escape_hatch() -> None:
    """Not every dashboard speaks the POST-with-token contract:
    tasks-dashboard's ``apiClient.getTasks()`` is a GET ``/tasks``
    that returns ``Task[]`` directly (no envelope, no total). The
    hook MUST accept a ``fetchFn`` escape hatch so the same state
    machine can drive non-POST endpoints without forcing every
    caller into the messages-shape."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    assert "fetchFn" in src, (
        "expected hook to accept a `fetchFn` escape hatch for "
        "non-POST endpoints (tasks-dashboard's apiClient.getTasks())"
    )


def test_use_paged_query_hook_supports_cache_ms() -> None:
    """``cacheMs`` (default 0 = no cache) — preserves the 30s cache
    tasks-dashboard's pre-migration ``useTasksData`` ran. Without
    the option the tasks page would re-fetch on every tab focus."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    assert "cacheMs" in src, (
        "expected hook to accept a `cacheMs` option for caching "
        "(tasks-dashboard's pre-migration useTasksData ran a 30s cache)"
    )


def test_use_paged_query_hook_returns_empty_array_safely() -> None:
    """The hook returns ``[]`` for ``data`` and ``0`` for ``total``
    when loading or errored — so consumers don't have to special-case
    ``data === null``."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    # The initial-state literal MUST be a real empty array (not null /
    # undefined) so the call-site .map() never crashes.
    assert re.search(r"useState\s*<[^>]*\bT\[\][^>]*>\s*\(\s*\[\s*\]\s*\)", src) or \
        re.search(r"useState\s*\(\s*\[\s*\]\s*as\s+T\[\]\s*\)", src), (
        "expected the data useState to default to [] (never null) so "
        "consumers don't have to guard .map()"
    )


# ---------- Consumer migrations ----------------------------------


def test_messages_dashboard_imports_use_paged_query() -> None:
    """messages-dashboard.tsx must import the hook after migration."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "usePagedQuery" in src, (
        "expected messages-dashboard.tsx to import usePagedQuery"
    )
    assert "use-paged-query" in src, (
        "expected messages-dashboard.tsx to reference '@/hooks/use-paged-query'"
    )


def test_messages_dashboard_no_longer_hand_rolls_query_fetch() -> None:
    """The bespoke ``callMessages('POST', '/query', …)`` listing fetch
    is the canonical thing this PR consolidates. After migration it
    must be replaced by the hook — the helper may survive for
    PATCH/DELETE/compose, but the ``'/query'`` invocation must NOT.
    """
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The exact suffix passed by the pre-migration listing call. If
    # this substring is still present, the hook isn't owning the
    # listing fetch.
    assert "'/query'" not in src and '"/query"' not in src, (
        "expected the bespoke `callMessages('POST', '/query', …)` "
        "fetch to be retired in favour of usePagedQuery owning the "
        "paginated listing"
    )


def test_messages_dashboard_no_longer_declares_messages_use_state() -> None:
    """The legacy ``useState<Message[]>([])`` (the rows-of-data slice)
    must be gone — the hook owns the data array now."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert not re.search(r"useState\s*<\s*Message\[\]\s*>", src), (
        "expected `useState<Message[]>([])` to be retired in favour "
        "of usePagedQuery owning the messages array"
    )


def test_tasks_dashboard_imports_use_paged_query() -> None:
    """tasks-dashboard.tsx must import the hook after migration —
    the in-file ``useTasksData`` delegates to it via the fetchFn
    escape hatch so the loading/error/lastFetch/refresh state
    machine comes from a single owner."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "usePagedQuery" in src, (
        "expected tasks-dashboard.tsx to import usePagedQuery"
    )
    assert "use-paged-query" in src, (
        "expected tasks-dashboard.tsx to reference '@/hooks/use-paged-query'"
    )


def test_tasks_dashboard_useTasksData_no_longer_calls_use_state_directly() -> None:
    """``useTasksData`` used to call ``useState`` four times (tasks /
    loading / error / lastFetch). After migration the hook owns the
    state machine — those four ``useState`` calls inside
    ``useTasksData`` must be gone (the wrapper can still memoize
    extras like ``isConnected``, but the fetch state comes from
    ``usePagedQuery``)."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # Pre-migration ``useTasksData`` body started with these four
    # state slots. Specifically these distinctive declarations.
    forbidden = [
        "useState<Task[]>([])",
        "useState<number>(0)",
    ]
    leaked = [f for f in forbidden if f in src]
    assert not leaked, (
        "expected the hand-rolled useState pattern inside useTasksData "
        f"to be replaced by usePagedQuery; still present: {leaked}"
    )


def test_tasks_dashboard_no_longer_owns_module_level_cache() -> None:
    """The module-level ``tasksCache`` Map was duplicated cache
    plumbing — once the hook owns ``cacheMs``, the call-site Map is
    redundant. Remove it so there's a single cache implementation
    in the dashboard codebase."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    # The distinctive module-level cache declaration.
    assert "const tasksCache = new Map" not in src, (
        "expected the module-level `tasksCache = new Map(...)` to be "
        "retired in favour of the hook's `cacheMs` option"
    )


# ---------- Out-of-scope marker ----------------------------------


def test_use_paged_query_hook_documents_agents_dashboard_scope() -> None:
    """agents-dashboard.tsx reads agents out of the global
    ``useDataStore`` (zustand) — a one-shot ``/api/all-data`` fetch
    multiplexed across every tab. It is NOT a per-tab paginated
    query and is intentionally out of scope for this hook. The
    hook's JSDoc must call this out so a future reader doesn't
    try to force the migration."""
    src = (DASHBOARD / "hooks" / "use-paged-query.ts").read_text()
    assert "useDataStore" in src or "data-store" in src or "agents-dashboard" in src, (
        "expected the hook's JSDoc to document why agents-dashboard.tsx "
        "is out of scope (it consumes the global useDataStore, not a "
        "per-tab paginated query endpoint)"
    )
