"""SEC round-20 (LOW, CONFIRMED-live): cross-agent advisory file-lock
STEAL via ``update_file_status(status="released")``.

Finding (AZ-R20-1): the "file already in use by another agent"
ownership check in :func:`update_file_status_tool_impl` carved out the
release path:

    if (
        resolved_abs_filepath in g.file_map
        and g.file_map[...].get("agent_id") != requesting_agent_id
        and new_status != "released"   # "Can always release, even if map is out of sync."
    ):
        return Conflict(...)

Because of ``and new_status != "released"``, a NON-holder worker could
call ``update_file_status(status="released")`` on a file locked by
ANOTHER agent — the code then unconditionally ``del``'d the
``g.file_map`` entry regardless of holder, stealing/clearing the other
agent's advisory lock (and freeing it to re-claim). Same class as the
round-19 task-tools foreign-object-mutation sweep, here in the file
surface.

Fix (best-practice, minimal): drop the ``and new_status != "released"``
carve-out so a NON-holder attempting to release a foreign-held file
gets the SAME ``Conflict`` a non-holder claim gets. The HOLDER still
releases their own lock (the ``agent_id != requesting_agent_id``
condition already makes the guard false when the requester IS the
holder). Operators never reach this tool (it is gated to
``kind == "agent_bearer"``), so there is no operator/admin force-release
path to preserve; the minimal correct fix stands.

RED on main: agent B releases A's lock, ``g.file_map`` entry is cleared
(steal succeeds). GREEN after: B gets Conflict and A's entry is
unchanged.
"""

from __future__ import annotations

import pytest

from agent_mcp.core import globals as g
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Conflict, Ok
from agent_mcp.tools.registry import dispatch_tool_call
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _worker(agent_id: str) -> Principal:
    """agent_bearer worker Principal — carries ``files.use``."""
    return make_principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role="worker",
        can_wake_loop=False,
        source_token="tok-" + agent_id,
    )


_FILEPATH = "/tmp/sec-r20-lock.txt"


async def _claim(principal: Principal, status: str = "editing", filepath: str = _FILEPATH):
    return await dispatch_tool_call(
        "update_file_status",
        {"filepath": filepath, "status": status},
        principal=principal,
    )


async def _release(principal: Principal, filepath: str = _FILEPATH):
    return await dispatch_tool_call(
        "update_file_status",
        {"filepath": filepath, "status": "released"},
        principal=principal,
    )


# ── The vulnerability: cross-agent release STEAL ─────────────────────


async def test_nonholder_cannot_release_foreign_lock(tmp_path) -> None:
    """RED on main: agent A locks a file; agent B (a different worker)
    calls ``update_file_status(release)`` on A's file. B must get a
    Conflict and A's lock in ``g.file_map`` must be UNCHANGED (still
    held by A). Pre-fix, B's release cleared A's entry (lock steal)."""
    async with mcp_session(tmp_path):
        alice = _worker("alice")
        bob = _worker("bob")

        claim = await _claim(alice, "editing")
        assert isinstance(claim, Ok), claim
        # Snapshot A's authoritative lock entry.
        held = g.file_map.get(claim.data["filepath"])
        assert held is not None and held["agent_id"] == "alice", held
        resolved = claim.data["filepath"]

        # B tries to release A's lock.
        stolen = await _release(bob, resolved)

        assert isinstance(stolen, Conflict), (
            f"non-holder release should Conflict, got {stolen!r}"
        )
        # Authoritative check: A's lock is still held by A, untouched.
        assert resolved in g.file_map, "A's lock was cleared by B (steal)"
        assert g.file_map[resolved]["agent_id"] == "alice", (
            f"A's lock holder changed: {g.file_map[resolved]!r}"
        )
        assert g.file_map[resolved] == held, "A's lock entry was mutated by B"


async def test_nonholder_cannot_reclaim_foreign_lock_via_other_status(
    tmp_path,
) -> None:
    """The same carve-out logic must not admit any OTHER status from a
    non-holder either. B attempting to claim A's file for 'reading'
    also Conflicts, and A's entry is unchanged (this path was already
    guarded pre-fix; pinned here as a regression guard)."""
    async with mcp_session(tmp_path):
        alice = _worker("alice")
        bob = _worker("bob")

        claim = await _claim(alice, "editing")
        assert isinstance(claim, Ok), claim
        resolved = claim.data["filepath"]
        held = dict(g.file_map[resolved])

        for status in ("reading", "editing", "reviewing"):
            res = await _claim(bob, status, resolved)
            assert isinstance(res, Conflict), (
                f"non-holder claim status={status} should Conflict, got {res!r}"
            )
            assert g.file_map[resolved] == held, (
                f"A's lock mutated by B claim status={status}"
            )


# ── Regressions: legitimate operations still work ───────────────────


async def test_holder_can_release_own_lock(tmp_path) -> None:
    """A can release its OWN lock — the ownership guard is false when
    the requester IS the holder, so self-release proceeds."""
    async with mcp_session(tmp_path):
        alice = _worker("alice")
        claim = await _claim(alice, "editing")
        assert isinstance(claim, Ok), claim
        resolved = claim.data["filepath"]

        released = await _release(alice, resolved)
        assert isinstance(released, Ok), released
        assert released.data["status"] == "released"
        assert resolved not in g.file_map, "A's own release did not clear the lock"


async def test_nonholder_can_claim_free_file(tmp_path) -> None:
    """B can claim a file that is genuinely free (no holder)."""
    async with mcp_session(tmp_path):
        bob = _worker("bob")
        free = "/tmp/sec-r20-free.txt"
        assert free not in g.file_map  # precondition

        res = await _claim(bob, "editing", free)
        assert isinstance(res, Ok), res
        assert g.file_map[res.data["filepath"]]["agent_id"] == "bob"


async def test_nonholder_can_release_file_it_holds(tmp_path) -> None:
    """B can release a file B itself holds (holder self-release)."""
    async with mcp_session(tmp_path):
        bob = _worker("bob")
        own = "/tmp/sec-r20-bob-owned.txt"
        claim = await _claim(bob, "editing", own)
        assert isinstance(claim, Ok), claim
        resolved = claim.data["filepath"]

        released = await _release(bob, resolved)
        assert isinstance(released, Ok), released
        assert resolved not in g.file_map


async def test_release_of_untracked_file_is_idempotent_ok(tmp_path) -> None:
    """Releasing a path that no agent holds stays an idempotent Ok
    (in_use=False) — the fix must not turn a free-path release into a
    Conflict (there is no holder to conflict with)."""
    async with mcp_session(tmp_path):
        bob = _worker("bob")
        untracked = "/tmp/sec-r20-never-tracked.txt"
        assert untracked not in g.file_map

        res = await _release(bob, untracked)
        assert isinstance(res, Ok), res
        assert res.data["in_use"] is False
