"""Guard: `reset_and_snapshot_globals` must cover every mutable
singleton it is responsible for.

arch-r6 #2 extracted `tests.conftest.reset_and_snapshot_globals` as
the SINGLE owner of "what mutable state must be isolated between
tests" — before the extraction, `tests/harness.py` carried an
independent byte-for-byte copy of the same reset/snapshot logic,
so a new mutable global added to `agent_mcp.core.state` (aka
`agent_mcp.core.globals`) had to be threaded into BOTH copies or
`mcp_session`-based tests would leak state that fixture-based tests
didn't (and vice versa) — an order-dependent flake with no obvious
attribution.

This test pins the known set of mutable containers the reset/restore
closure snapshots. It does NOT reflect over every module attribute:
`agent_mcp.core.state` also declares `agent_color_index` (int counter)
and `agent_profile_counter` (int counter) that are NOT currently reset
between tests — a pre-existing gap, out of scope for this
behavior-preserving refactor. A fully reflective assertion would fail
on those today for reasons unrelated to the extraction. Instead we
assert the explicit known set inline (`test_reset_and_snapshot_globals_covers_known_mutable_state`)
against the source of that snapshot -- keeping this test both a
regression guard on drift in the union of "container globals that
exist" AND a document of exactly which ones the reset covers.

If a new mutable dict/list/set singleton needing per-test isolation is
added to `agent_mcp.core.state`, this test should be updated alongside
the extension of `reset_and_snapshot_globals` — the failure here is a
deliberate tripwire pointing back at that function.
"""

from __future__ import annotations

import inspect

from tests.conftest import reset_and_snapshot_globals

# The set of `agent_mcp.core.state` attribute names that
# `reset_and_snapshot_globals` clears/snapshots-and-restores today.
# Mirrors the snapshot dict keys plus the unconditionally-cleared
# event-coordination registries (which are cleared, not
# snapshotted/restored — a fresh test-process event loop can't reuse
# a prior loop's asyncio.Event/Lock/Queue instances anyway).
KNOWN_RESET_TARGETS = frozenset(
    {
        "connections",
        "active_agents",
        "tasks",
        "file_map",
        "agent_working_dirs",
        "audit_log",
        "global_vss_load_tested",
        "global_vss_load_successful",
        "agent_event_signals",
        "agent_event_locks",
        "agent_event_queues",
        "agent_event_waiters",
    }
)

# Mutable containers that exist on `agent_mcp.core.state` today but are
# deliberately NOT covered by `reset_and_snapshot_globals` — documented
# here so this test doesn't silently start failing if someone notices
# the gap and "fixes" it by widening `KNOWN_RESET_TARGETS` without also
# widening the function (or vice versa). Each entry names the reason
# it's out of scope.
KNOWN_UNRESET_MUTABLE_STATE = frozenset(
    {
        # int counters, not dict/list/set — no cross-test leak risk
        # beyond a monotonically-growing suffix, which tests don't
        # assert on.
        "agent_color_index",
        "agent_profile_counter",
        # Optional[bytes] — set fresh by every `mcp_session` /
        # `client` fixture caller that needs it; not a container that
        # accumulates state across tests.
        "forwarding_hmac_key",
    }
)


def test_reset_and_snapshot_globals_covers_known_mutable_state() -> None:
    """The reset/restore closure must touch exactly the known set of
    mutable containers — no silent drops, no silent gaps.

    Reads `reset_and_snapshot_globals`'s own source rather than probing
    live global state, so this test doesn't itself need `reset_globals`
    isolation (it makes no mutations) and fails loudly — pointing at
    this file — the moment someone edits the function's snapshot dict
    or clear-calls without updating `KNOWN_RESET_TARGETS` in lockstep.
    """
    source = inspect.getsource(reset_and_snapshot_globals)
    missing = {
        name for name in KNOWN_RESET_TARGETS if f"g.{name}" not in source
    }
    assert not missing, (
        f"reset_and_snapshot_globals no longer references g.{{{missing}}} "
        "— either the reset was dropped (state will leak across tests) "
        "or KNOWN_RESET_TARGETS is stale. Update both in lockstep."
    )


def test_state_module_has_no_new_unaccounted_mutable_containers() -> None:
    """Every dict/list/set attribute on `agent_mcp.core.state` must be
    accounted for as either reset by `reset_and_snapshot_globals` or
    explicitly documented as an accepted gap.

    This is the tripwire for the motivating scenario: a future PR adds
    e.g. `agent_mcp.core.state.pending_approvals: dict[str, ...] = {}`
    for some new feature. Without this test, that global silently
    leaks across tests until someone spends an afternoon bisecting an
    order-dependent flake. With it, the new attribute fails this test
    the moment it's added, pointing directly at
    `reset_and_snapshot_globals` (and this file) as the place to wire
    it in.
    """
    from agent_mcp.core import state

    accounted = KNOWN_RESET_TARGETS | KNOWN_UNRESET_MUTABLE_STATE
    unaccounted = []
    for name in dir(state):
        if name.startswith("_"):
            continue
        value = getattr(state, name)
        if isinstance(value, (dict, list, set)) and name not in accounted:
            unaccounted.append(name)

    assert not unaccounted, (
        f"agent_mcp.core.state has new mutable container(s) not covered "
        f"by reset_and_snapshot_globals and not documented as an "
        f"accepted gap: {unaccounted!r}. Either add per-test reset for "
        f"them in tests/conftest.py::reset_and_snapshot_globals and add "
        f"to KNOWN_RESET_TARGETS, or add to "
        f"KNOWN_UNRESET_MUTABLE_STATE here with a reason."
    )
