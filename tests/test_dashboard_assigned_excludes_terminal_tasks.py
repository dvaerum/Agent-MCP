"""The Agents page row count labelled "N assigned" must mean
"open / still-to-do tasks assigned to this agent", NOT "total tasks
that were ever assigned to this agent" — the latter is what the
current code computes, which is why ios-app-dev shows 18 "assigned"
in washing-brothers when 16 of them are completed.

This is a UI-only filter: the dashboard already has the task rows
locally (it gets them from /api/all-data). The fix is to AND the
existing `assigned_to` filter with `status NOT IN
('completed', 'cancelled', 'failed')` in TWO places in
`agents-dashboard.tsx`:

  1. `taskStats.assigned` (line ~102) — drives the row's text
     "{n} assigned" pill on the Agents table.
  2. The same filter pattern duplicated inside the row body (line
     ~89-93).

This regression guard greps the source file for the predicate
(text-parse pattern matches the other dashboard tests in this
repo — see test_dashboard_agents_popup_polish.py — because the
project ships no jsdom / vitest setup).
"""

from __future__ import annotations

import re

from tests.dashboard_sources import agents_page_source


def _src() -> str:
    # The row markup moved out of agents-dashboard.tsx into the Agents
    # column spec when the page adopted <DataTablePage>; these guards
    # are about the page's behaviour, so they read the whole page +
    # satellites (tests/dashboard_sources.py).
    return agents_page_source()


# All three terminal statuses must appear as exclusions in the
# assigned-tasks filter — otherwise the count keeps over-reporting.
TERMINAL_STATUSES = ("completed", "cancelled", "failed")


def test_assignedtasks_filter_excludes_terminal_statuses() -> None:
    """The `assignedTasks` filter block in agents-dashboard.tsx must
    exclude tasks whose status is one of completed / cancelled /
    failed. We assert the file contains a recognisable predicate
    naming all three statuses near the `assignedTasks` declaration."""
    src = _src()
    # Locate the assignedTasks block and look at a window of source
    # around it.
    m = re.search(r"const\s+assignedTasks\s*=", src)
    assert m is not None, (
        "expected `const assignedTasks = ...` in agents-dashboard.tsx — "
        "did the row-level filter move?"
    )
    # The predicate uses `agentTasks.filter(t => ...)`; check the
    # next ~400 chars include the status exclusions.
    window = src[m.start(): m.start() + 600]
    for status in TERMINAL_STATUSES:
        assert f"'{status}'" in window or f'"{status}"' in window, (
            f"assignedTasks filter does not reference {status!r} — "
            f"completed/cancelled/failed tasks will keep being counted "
            f"as assigned. Window:\n{window}"
        )


def test_taskstats_assigned_counts_only_open_tasks() -> None:
    """`taskStats.assigned` is what populates the row's '{n} assigned'
    label. It must be derived from the filtered list, not from the
    raw assigned_to-only filter. We assert the file is consistent:
    if `assigned: assignedTasks.length` appears, the assignedTasks
    block above must already filter out terminal statuses (covered
    by the previous test); additionally, there must not be any
    second un-filtered `t.assigned_to ===` predicate that also feeds
    a count display."""
    src = _src()
    # The display line `{taskStats.assigned > 0 && \`${taskStats.assigned}
    # assigned\`}` must exist (we don't want a refactor to silently
    # bypass our filter).
    assert "taskStats.assigned" in src, (
        "taskStats.assigned not found — the row's assigned-count label "
        "must keep using the filtered count, not recompute from raw "
        "agentTasks"
    )
    # Belt-and-braces: every line that filters `t.assigned_to ===`
    # to compute a count for display should sit next to a status
    # exclusion. Find every occurrence and verify nearby context.
    for match in re.finditer(r"\.filter\(\s*t\s*=>\s*[^)]*t\.assigned_to", src):
        end = match.end()
        # Look at the next 400 chars for at least one of the three
        # terminal statuses being excluded.
        window = src[match.start(): end + 400]
        has_exclusion = any(
            f"'{s}'" in window or f'"{s}"' in window
            for s in TERMINAL_STATUSES
        )
        assert has_exclusion, (
            f"found a .filter(t => ... t.assigned_to ...) block that "
            f"does not reference any terminal status — it will "
            f"over-count finished tasks. Block context:\n{window}"
        )
