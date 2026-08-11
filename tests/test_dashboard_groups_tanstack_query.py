"""Regression guards for the groups-list migration onto TanStack Query.

History
-------
The router-admin surface (users / groups / SSO / memberships /
capabilities) rode the hand-rolled ``useRouterQuery`` hook — a
``{data, loading, error, forbidden, refresh}`` state machine, the
router-admin sibling of the retired ``usePagedQuery``.

W6-followup F4 moved the GROUPS LIST read (``GET
/agent-mcp/api/router/groups``) off ``useRouterQuery`` and onto the
shared TanStack Query ``queryClient`` via ``useGroupsQuery``
(``lib/queries/groups.ts``), mirroring the F2 (tasks) / F3 (messages)
migrations. Two things make groups deliberately different from the
per-project lists, and this guard pins both:

  * ROUTER-level key. ``groupsQueryKey()`` is a bare ``['groups']`` with
    NO project segment (contrast ``['tasks', project, …]`` /
    ``['messages', project, …]``) — there is one groups list per router.

  * NO SSE poll / invalidation. The groups page renders at the
    cross-project overview, which has no operator-events SSE stream, so
    freshness after a group mutation rides an explicit
    ``invalidateGroups()`` from each mutation's success handler rather
    than the debounced SSE choke point the per-project lists use.

``useRouterQuery`` itself is NOT retired: SSO, users, project
memberships and the per-group capabilities section still use it. So the
guard repoints the GROUPS PAGE off it while asserting the hook survives
for its other consumers — the mirror of
``test_dashboard_use_paged_query_hook.py`` but scoped to groups.

These are text-parse regression guards (same convention as
``test_dashboard_use_paged_query_hook.py``); behaviour is verified by the
vitest suites (``groups-query-invalidation.test.ts`` +
``groups-dashboard.test.tsx``) plus ``npm run build``.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- The groups query module + key/invalidation seam ----------


def test_groups_query_module_uses_tanstack_query() -> None:
    """``lib/queries/groups.ts`` must exist and ride ``useQuery`` keyed by
    ``groupsQueryKey`` — the same shape as ``lib/queries/tasks.ts`` /
    ``lib/queries/messages.ts``."""
    src = _read("lib/queries/groups.ts")
    assert "useGroupsQuery" in src, (
        "expected lib/queries/groups.ts to export useGroupsQuery"
    )
    assert "useQuery" in src, (
        "expected useGroupsQuery to ride TanStack Query's useQuery"
    )
    assert "groupsQueryKey" in src, (
        "expected useGroupsQuery to key on groupsQueryKey (from "
        "lib/query-client.ts)"
    )


def test_query_client_exposes_groups_key_and_invalidator() -> None:
    """``lib/query-client.ts`` must own the router-level groups key +
    invalidator. ``groupsQueryKey`` is a bare ``['groups']`` (no project
    segment — groups are router-level, not per-project), and
    ``invalidateGroups`` is the manual freshness choke point the group
    mutations call."""
    src = _read("lib/query-client.ts")
    assert "export const groupsQueryKey" in src, (
        "expected lib/query-client.ts to export groupsQueryKey"
    )
    assert "export function invalidateGroups" in src, (
        "expected lib/query-client.ts to export invalidateGroups()"
    )
    # The key must carry NO project segment — a bare ['groups'].
    assert re.search(
        r"groupsQueryKey\s*=\s*\(\s*\)\s*=>\s*\[\s*GROUPS_KEY\s*\]",
        src,
    ), (
        "expected groupsQueryKey() to be a bare [GROUPS_KEY] — groups are "
        "a ROUTER-level resource and must NOT be project-namespaced like "
        "tasksQueryKey / messagesQueryKey"
    )


# ---------- The groups page migrated off useRouterQuery --------------


def test_groups_dashboard_migrated_to_tanstack_query() -> None:
    """W6-followup F4: groups-dashboard.tsx no longer rides
    ``useRouterQuery`` for its list — it imports the TanStack
    ``useGroupsQuery`` (``lib/queries/groups.ts``) and wires
    ``invalidateGroups`` as the post-mutation freshness path.

    Repointed — NOT weakened — from the old router-query import to the new
    location: the page must import the TanStack groups query and must NOT
    ``import`` the retired hook module (an incidental doc-comment naming
    the hook to explain lineage is fine)."""
    src = _read("components/dashboard/groups-dashboard.tsx")
    assert "useGroupsQuery" in src, (
        "expected groups-dashboard.tsx to import useGroupsQuery (the "
        "TanStack Query groups-list fetch)"
    )
    assert "lib/queries/groups" in src, (
        "expected groups-dashboard.tsx to reference '@/lib/queries/groups'"
    )
    assert "invalidateGroups" in src, (
        "expected groups-dashboard.tsx to call invalidateGroups() as the "
        "post-mutation freshness path (no SSE at the overview)"
    )
    # A REAL import of the retired hook — NOT the doc-comment mentions
    # that describe the migration's lineage.
    assert not re.search(
        r"""from\s+['"][^'"]*use-router-query['"]""", src
    ), (
        "expected groups-dashboard.tsx to NOT import '@/hooks/use-router-query' "
        "after the F4 migration onto TanStack Query"
    )


# ---------- useRouterQuery survives for its other consumers ----------


def test_use_router_query_hook_is_retained() -> None:
    """``useRouterQuery`` is scoped-out of the groups PAGE only — it is
    NOT retired. SSO, users, project memberships and the per-group
    capabilities section still ride it, so the hook file must remain and
    stay imported by those consumers. (Contrast the fully-retired
    ``use-paged-query.ts``.)"""
    path = DASHBOARD / "hooks" / "use-router-query.ts"
    assert path.exists(), (
        "hooks/use-router-query.ts must remain — it still owns the fetch "
        "state machine for sso-dashboard / users-dashboard / "
        "project-memberships-modal / group-capabilities-section"
    )
    consumers = [
        "components/dashboard/sso-dashboard.tsx",
        "components/dashboard/users-dashboard.tsx",
        "components/dashboard/project-memberships-modal.tsx",
        "components/dashboard/groups/group-capabilities-section.tsx",
    ]
    for rel in consumers:
        src = _read(rel)
        assert re.search(
            r"""from\s+['"][^'"]*use-router-query['"]""", src
        ), (
            f"expected {rel} to still import '@/hooks/use-router-query' "
            "(the groups migration keeps it for the remaining router-admin "
            "consumers)"
        )
