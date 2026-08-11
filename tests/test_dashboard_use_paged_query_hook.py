"""Regression guards for the retirement of the ``usePagedQuery<T>`` hook.

History
-------
PR 5 of the 2026-06-09 architecture review introduced ``usePagedQuery`` —
a single owner of the paginated-fetch state machine
(``{data, total, loading, error, refresh, lastFetch}``) that the tasks
and messages dashboards each used to hand-roll. The Wave 6 follow-up then
moved BOTH consumers onto the shared TanStack Query ``queryClient``:

- F2 migrated ``tasks-dashboard.tsx`` onto ``useTasksQuery``
  (``lib/queries/tasks.ts``) — one query per ``['tasks', project,
  filters]``.
- F3 migrated ``messages-dashboard.tsx`` onto ``useMessagesQuery``
  (``lib/queries/messages.ts``) — one query per ``['messages', project,
  {filters, limit, offset}]``.

With the last consumer gone, ``hooks/use-paged-query.ts`` had no importers
left, so F3 DELETED it. This guard is repointed — NOT weakened — from
"the hook exists and both pages use it" to "the hook is gone and both
pages ride TanStack Query". The single-source-of-truth property the hook
was created to deliver now lives in the shared ``queryClient``; a
reintroduction of the bespoke state machine would be the regression.

These tests are text-parse regression guards (same convention as
``test_dashboard_use_filters_hook.py``); the fork verifies behaviour via
the vitest suites (``tasks-query-*.test.*`` / ``messages-query-*.test.*``)
plus ``npm run build``.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The hook is retired ----------------------------------


def test_use_paged_query_hook_file_is_removed() -> None:
    """``hooks/use-paged-query.ts`` must be GONE — its last consumer
    (messages-dashboard) migrated onto TanStack Query in F3, leaving no
    importers. A resurrected file means the bespoke paginated-fetch state
    machine crept back alongside the shared ``queryClient``."""
    path = DASHBOARD / "hooks" / "use-paged-query.ts"
    assert not path.exists(), (
        f"expected {path} to be deleted (all consumers migrated to "
        "TanStack Query); the hand-rolled paginated-fetch hook must not "
        "be reintroduced"
    )


def test_no_source_imports_use_paged_query() -> None:
    """No .ts/.tsx source may import the retired hook. A lingering import
    would fail the build (the file is gone) — this catches it at the grep
    layer with a clearer message, and pins that neither a real import nor
    a ``vi.mock`` of the hook survives."""
    offenders: list[str] = []
    for path in DASHBOARD.rglob("*.ts*"):
        if "node_modules" in path.parts:
            continue
        text = path.read_text()
        # A real import / mock of the hook module — NOT the incidental
        # historical mentions in doc-comments (which describe lineage).
        if re.search(r"""from\s+['"][^'"]*use-paged-query['"]""", text) or re.search(
            r"""vi\.mock\(\s*['"][^'"]*use-paged-query['"]""", text
        ):
            offenders.append(str(path.relative_to(DASHBOARD)))
    assert not offenders, (
        "expected no module to import '@/hooks/use-paged-query' after "
        f"its removal; still referenced by: {offenders}"
    )


# ---------- Consumer migrations (TanStack Query) -----------------


def test_messages_dashboard_migrated_to_tanstack_query() -> None:
    """W6-followup F3: messages-dashboard.tsx no longer rides the
    hand-rolled ``usePagedQuery`` state machine — the list fetch moved
    onto the shared TanStack Query client via ``useMessagesQuery`` (see
    ``lib/queries/messages.ts``), keyed ``['messages', project, {filters,
    limit, offset}]`` with one SSE invalidation choke point.

    This guard was previously ``test_messages_dashboard_imports_use_paged_query``
    (which asserted the OLD import). Repointed — NOT weakened — to the new
    location: the page must import the TanStack messages query and must
    NOT re-introduce the retired hook."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "useMessagesQuery" in src, (
        "expected messages-dashboard.tsx to import useMessagesQuery (the "
        "TanStack Query messages-list fetch)"
    )
    assert "lib/queries/messages" in src, (
        "expected messages-dashboard.tsx to reference '@/lib/queries/messages'"
    )
    assert "usePagedQuery" not in src, (
        "expected messages-dashboard.tsx to NOT import usePagedQuery after "
        "the F3 migration onto TanStack Query"
    )
    assert "use-paged-query" not in src, (
        "expected messages-dashboard.tsx to NOT reference "
        "'@/hooks/use-paged-query' after the F3 migration"
    )


def test_messages_dashboard_no_longer_declares_messages_use_state() -> None:
    """The legacy ``useState<Message[]>([])`` (the rows-of-data slice)
    must be gone — the TanStack query owns the messages array now."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert not re.search(r"useState\s*<\s*Message\[\]\s*>", src), (
        "expected `useState<Message[]>([])` to be retired in favour "
        "of useMessagesQuery owning the messages array"
    )


def test_messages_dashboard_retires_the_page_poll_and_window_listener() -> None:
    """The pre-migration page ran its own 60s ``setInterval`` background
    poll and an ``mcp:resources-updated`` window listener to refetch the
    listing. Both are replaced by the single ``invalidateMessages()`` SSE
    choke point (``lib/mcp-notifications.ts``); they must be gone from the
    page so there is ONE freshness path, not three."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Match actual CODE usage (with the call paren) — a doc-comment that
    # names the retired mechanism to explain WHY it is gone is fine.
    assert "setInterval(" not in src, (
        "expected the 60s setInterval background poll to be retired in "
        "favour of the SSE-driven invalidateMessages() refetch"
    )
    assert "addEventListener(" not in src, (
        "expected the mcp:resources-updated window listener to be retired "
        "in favour of the SSE-driven invalidateMessages() refetch"
    )


def test_messages_list_fetch_lives_in_the_api_layer() -> None:
    """The listing POST to ``/messages/query`` moved out of the retired
    hook and into the api layer as ``getMessages`` — the same shape
    ``useTasksQuery`` gets from ``getTasks``. Pin that the api module owns
    the endpoint + the POST verb (the GET-with-body bug that birthed
    /messages/query must stay buried)."""
    api_src = _read("lib/api/messages.ts")
    assert "getMessages" in api_src, (
        "expected lib/api/messages.ts to export getMessages (the "
        "paginated messages-list reader)"
    )
    assert "/messages/query" in api_src, (
        "expected getMessages to POST to '/messages/query'"
    )
    assert re.search(r"method\s*:\s*['\"]POST['\"]", api_src), (
        "expected getMessages to use method: 'POST' (browsers strip GET "
        "bodies — the original bug)"
    )


def test_tasks_dashboard_migrated_to_tanstack_query() -> None:
    """W6-followup F2 (kept green through F3): tasks-dashboard.tsx rides
    the TanStack ``useTasksQuery`` and must NOT re-introduce the retired
    hook."""
    src = _read("components/dashboard/tasks-dashboard.tsx")
    assert "useTasksQuery" in src, (
        "expected tasks-dashboard.tsx to import useTasksQuery"
    )
    assert "lib/queries/tasks" in src, (
        "expected tasks-dashboard.tsx to reference '@/lib/queries/tasks'"
    )
    assert "usePagedQuery" not in src, (
        "expected tasks-dashboard.tsx to NOT import usePagedQuery"
    )
    assert "use-paged-query" not in src, (
        "expected tasks-dashboard.tsx to NOT reference "
        "'@/hooks/use-paged-query'"
    )
