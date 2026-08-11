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

W6-followup-2 G2 then migrated the LAST four ``useRouterQuery``
consumers — SSO, users, project memberships and the per-group
capabilities section — onto their own TanStack Query modules
(``lib/queries/{sso,users,project-memberships,group-capabilities}.ts``),
each with a bare-or-parent-scoped key + ``invalidateX()`` in
``lib/query-client.ts``. With zero consumers left, the hand-rolled hook
(``hooks/use-router-query.ts``) and its vitest guard were DELETED —
exactly the fully-retired shape of ``use-paged-query.ts``. So the guard
below now asserts the hook is GONE and every former consumer imports its
TanStack replacement instead.

These are text-parse regression guards (same convention as
``test_dashboard_use_paged_query_hook.py``); behaviour is verified by the
vitest suites (``groups-query-invalidation.test.ts`` +
``router-admin-queries-invalidation.test.ts`` + ``groups-dashboard.test.tsx``)
plus ``npm run build``.
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


# ---------- useRouterQuery is fully retired (G2) ----------------------


# Each former ``useRouterQuery`` consumer → the TanStack Query module it
# now imports (`lib/queries/<name>.ts`). Repointed, NOT weakened: the
# assertion moved from "still imports the hook" to "imports its migration
# target and no longer imports the deleted hook".
_MIGRATED_CONSUMERS = {
    "components/dashboard/sso-dashboard.tsx": "lib/queries/sso",
    "components/dashboard/users-dashboard.tsx": "lib/queries/users",
    "components/dashboard/project-memberships-modal.tsx": (
        "lib/queries/project-memberships"
    ),
    "components/dashboard/groups/group-capabilities-section.tsx": (
        "lib/queries/group-capabilities"
    ),
}


def test_use_router_query_hook_is_removed() -> None:
    """W6-followup-2 G2: with the last four consumers migrated onto
    TanStack Query, the hand-rolled ``useRouterQuery`` hook + its vitest
    guard are DELETED — the fully-retired shape of ``use-paged-query.ts``.
    Nothing may import the hook module any more."""
    hook = DASHBOARD / "hooks" / "use-router-query.ts"
    assert not hook.exists(), (
        "hooks/use-router-query.ts must be DELETED after G2 migrated its "
        "last consumers (users / SSO / memberships / capabilities) onto "
        "TanStack Query"
    )
    guard = DASHBOARD / "tests" / "use-router-query.test.ts"
    assert not guard.exists(), (
        "tests/use-router-query.test.ts (the resolveRouterQuery guard) must "
        "be DELETED alongside the hook it tested"
    )


def test_former_router_query_consumers_migrated_to_tanstack() -> None:
    """Every former ``useRouterQuery`` consumer imports its TanStack Query
    module (``lib/queries/<name>``) and no longer imports the deleted
    hook. Incidental doc-comment mentions of the old hook's name (to
    explain migration lineage) are fine — only a real ``import`` is
    forbidden."""
    for rel, query_module in _MIGRATED_CONSUMERS.items():
        src = _read(rel)
        assert query_module in src, (
            f"expected {rel} to import '@/{query_module}' (its TanStack "
            "Query replacement for useRouterQuery)"
        )
        assert not re.search(
            r"""from\s+['"][^'"]*use-router-query['"]""", src
        ), (
            f"expected {rel} to NOT import '@/hooks/use-router-query' after "
            "the G2 migration onto TanStack Query"
        )


def test_query_client_exposes_router_admin_keys_and_invalidators() -> None:
    """``lib/query-client.ts`` owns the router-level key + invalidator for
    each migrated resource, matching the groups seam. Users/SSO are single
    router-level resources (bare keys); memberships/capabilities are keyed
    by their parent id."""
    src = _read("lib/query-client.ts")
    for symbol in (
        "export const usersQueryKey",
        "export function invalidateUsers",
        "export const ssoConfigQueryKey",
        "export function invalidateSsoConfig",
        "export const projectMembershipsQueryKey",
        "export function invalidateProjectMemberships",
        "export const groupCapabilitiesQueryKey",
        "export function invalidateGroupCapabilities",
    ):
        assert symbol in src, (
            f"expected lib/query-client.ts to declare `{symbol}` (G2 "
            "router-admin migration)"
        )
