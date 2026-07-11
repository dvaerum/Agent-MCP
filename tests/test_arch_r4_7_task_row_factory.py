"""arch-deepening R4 #7 — single task-row factory + opaque id minting.

Before this PR, ``agent_mcp/tools/task_tools.py`` carried ~7 near-
identical ``task_repo.create({...})`` call sites, each hand-listing
the same defaulted columns (``child_tasks``/``depends_on_tasks``/
``notes`` = ``[]``, ``status``/``priority`` defaults) that
``TaskRepository.create`` already applies via ``.get()`` — and THREE
incompatible id generators fed those calls:

* ``task_tools._generate_task_id`` — opaque, ``secrets``-based.
* ``f"task_{int(now().timestamp()*1000)}"`` (single unassigned) —
  two calls in the same millisecond mint the SAME id, so the second
  INSERT trips the ``task_id`` primary key and raises.
* ``f"task_{int(now().timestamp()*1000)}_{i}"`` (multi-create) — safe
  WITHIN one call (``i`` differs per row) but two separate multi-
  create calls in the same millisecond can still collide on ``i=0``.

This PR retires both timestamp variants: id-minting moves into
``TaskRepository.create`` itself (mints via the opaque scheme when
the caller omits ``task_id``), and every non-timestamp call site that
doesn't need the id before the row exists now lets the repo mint it.

This file pins the collision fix at the tool-call boundary (not just
the repo unit level, which ``tests/test_task_repository.py`` covers)
by reproducing the ORIGINAL bug conditions: two Mode-0 "create an
unassigned task" calls with a FROZEN clock, so both would have
computed the identical timestamp-derived id under the retired scheme.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from agent_mcp.core.tool_result import Ok
from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


class _FrozenDateTime(_dt.datetime):
    """A ``datetime.datetime`` subclass whose ``.now()`` never advances.

    Reproduces "two creates land in the same millisecond" deterministically
    instead of relying on real clock timing (which is flaky — DB I/O
    between the two calls usually pushes them past 1ms on real hardware).
    """

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - stdlib signature
        return cls(2026, 1, 1, 12, 0, 0, 0, tzinfo=tz)


async def test_unassigned_task_ids_dont_collide_same_millisecond(
    tmp_path, monkeypatch,
) -> None:
    """RED against the retired timestamp scheme, GREEN against the repo mint.

    Freezes ``datetime.datetime.now()`` inside ``task_tools`` and fires
    two independent Mode-0 unassigned-task creations. Under the retired
    ``task_{int(now().timestamp()*1000)}`` generator this reproducibly
    raised ``sqlite3.IntegrityError`` (duplicate primary key) on the
    second call, surfaced by the impl as a ``Failed`` result. With
    id-minting moved into ``task_repo.create``, both calls succeed with
    distinct ids regardless of the caller's clock.
    """
    from agent_mcp.tools.task_tools import _create_unassigned_tasks

    async with mcp_session(tmp_path):
        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.datetime.datetime", _FrozenDateTime,
        )

        result_1 = await _create_unassigned_tasks(
            {"task_title": "first frozen-instant task", "task_description": "d1"}
        )
        result_2 = await _create_unassigned_tasks(
            {"task_title": "second frozen-instant task", "task_description": "d2"}
        )

        assert isinstance(result_1, Ok), result_1
        assert isinstance(result_2, Ok), result_2

        id_1 = re.findall(r"task_[a-f0-9]+", result_1.message or "")
        id_2 = re.findall(r"task_[a-f0-9]+", result_2.message or "")
        assert id_1 and id_2, (result_1, result_2)
        assert set(id_1).isdisjoint(id_2), (
            f"two same-millisecond unassigned-task creates minted "
            f"colliding ids: {id_1} vs {id_2}"
        )


async def test_multi_create_ids_dont_collide_same_millisecond(
    tmp_path, monkeypatch,
) -> None:
    """Same reproduction for the multi-create loop's retired
    ``task_{int(now().timestamp()*1000)}_{i}`` generator: two calls to
    ``_create_and_assign_multiple_tasks`` at the same frozen instant
    must not collide on the ``i=0`` (or any) row.
    """
    from agent_mcp.tools.task_tools import _create_and_assign_multiple_tasks

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")

        monkeypatch.setattr(
            "agent_mcp.tools.task_tools.datetime.datetime", _FrozenDateTime,
        )

        result_1 = await _create_and_assign_multiple_tasks(
            {},
            alice.agent_id,
            [{"title": "batch A #1", "description": "d"}],
            False,
            False,
            "",
        )
        result_2 = await _create_and_assign_multiple_tasks(
            {},
            alice.agent_id,
            [{"title": "batch B #1", "description": "d"}],
            False,
            False,
            "",
        )

        assert isinstance(result_1, Ok), result_1
        assert isinstance(result_2, Ok), result_2

        id_1 = re.findall(r"task_[a-f0-9]+", result_1.message or "")
        id_2 = re.findall(r"task_[a-f0-9]+", result_2.message or "")
        assert id_1 and id_2, (result_1, result_2)
        assert set(id_1).isdisjoint(id_2), (
            f"two same-millisecond multi-create calls minted colliding "
            f"ids: {id_1} vs {id_2}"
        )
