"""W6-followup-2 G1 — single `/all-data` invalidation per mutation.

The `/all-data`-backed pages (Memories, Agents) used to `await
refreshData()` in every create/update/delete success handler AND still
receive the backend `resources/updated` echo the SSE choke point turns
into `invalidateAllData()` — TWO `/all-data` refetches per mutation.

The fix routes each handler's own post-write signal through the SAME
debounced choke point the echo uses (`scheduleDashboardRefresh`), so the
operator's own write coalesces with the echo into exactly ONE refetch.
These grep guards pin the convention so a future edit doesn't reintroduce
the imperative refetch (which would restore the double-fetch):

  * the mutation handlers call `scheduleDashboardRefresh()`;
  * they do NOT call `refreshData()` imperatively (`await`/`void`) — that
    hook is reserved for the manual Refresh button (`onRefresh`).

The behavioural counterpart (the two signals coalescing into one
invalidation) is asserted in the vitest suite
`tests/mutation-single-invalidation.test.tsx`.
"""

from __future__ import annotations

import pytest

from tests.dashboard_sources import agents_page_source, read_dashboard

MEMORIES_PAGE = "components/dashboard/memories-dashboard.tsx"


@pytest.fixture(scope="module")
def memories_src() -> str:
    return read_dashboard(MEMORIES_PAGE)


@pytest.fixture(scope="module")
def agents_src() -> str:
    return agents_page_source()


def test_memories_mutations_route_through_shared_choke_point(memories_src: str) -> None:
    assert "scheduleDashboardRefresh" in memories_src, (
        "Memories mutation handlers must signal via the shared debounced "
        "`scheduleDashboardRefresh()` choke point so the operator's write "
        "coalesces with the backend echo into ONE /all-data refetch."
    )


def test_agents_mutations_route_through_shared_choke_point(agents_src: str) -> None:
    assert "scheduleDashboardRefresh" in agents_src, (
        "Agents mutation handlers must signal via the shared debounced "
        "`scheduleDashboardRefresh()` choke point (was `await refreshData()`)."
    )


@pytest.mark.parametrize("src_name", ["memories", "agents"])
def test_no_imperative_all_data_refetch_in_mutation_handlers(
    src_name: str, memories_src: str, agents_src: str
) -> None:
    src = memories_src if src_name == "memories" else agents_src
    # The imperative `/all-data` refetch (`await refreshData()` /
    # `void refreshData()`) is the double-fetch source. It must be gone
    # from the mutation paths; `refreshData` survives ONLY as the manual
    # `onRefresh` button binding.
    assert "await refreshData()" not in src, (
        f"{src_name}: `await refreshData()` reintroduces the double "
        "/all-data fetch — signal via `scheduleDashboardRefresh()` instead."
    )
    assert "void refreshData()" not in src, (
        f"{src_name}: `void refreshData()` reintroduces the double "
        "/all-data fetch — signal via `scheduleDashboardRefresh()` instead."
    )


@pytest.mark.parametrize("src_name", ["memories", "agents"])
def test_manual_refresh_button_preserved(
    src_name: str, memories_src: str, agents_src: str
) -> None:
    src = memories_src if src_name == "memories" else agents_src
    assert "onRefresh: refreshData" in src, (
        f"{src_name}: the manual Refresh button (`onRefresh: refreshData`) "
        "must stay wired to the awaitable force-refetch."
    )
