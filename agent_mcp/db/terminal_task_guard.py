# Agent-MCP/agent_mcp/db/terminal_task_guard.py
"""Shared marker + exception for the DB-level terminal-task guard.

OBS-R12-2: "terminal-state carve-out miss" is a recurring bug class — a
task write path forgets that a completed/cancelled/failed task is a
frozen sink, because the invariant
(``task_tools._TERMINAL_TASK_STATUSES`` /
``_is_status_transition_allowed``) was enforced opt-in, per call-site,
in Python. Migration ``0025_terminal_task_guard_trigger`` installs a
structural backstop one layer below every Python call-site: a pair of
SQLite ``TRIGGER``s (one on ``tasks``, two on the ``task_comments``
side table — formerly ``task_notes``, renamed in migration 0026; see
the migration docstring for the exact field/table coverage and the
class-sweep that added the side-table pair) that refuse the write at
the DB layer, so a future write path can't forget the check even if
it never heard of the Python-level convention.

Two writers can observe the trigger's ``RAISE(ABORT, ...)``:

* ``agent_mcp.repositories.task_repository`` (the ``tasks`` table
  writers — ``update_fields`` / ``_update_fields_with_cursor`` /
  ``update_task_fields_in_db``).
* ``agent_mcp.db.actions.task_comments_db`` (the ``task_comments``
  side table writers — ``add_comment`` / ``edit_comment``).

Both import this one module so the marker string and the exception
type can't drift between the two writers and the migration that
raises it.
"""

from __future__ import annotations

# Must match the literal embedded in the trigger's
# ``RAISE(ABORT, '<marker>: ...')`` message in
# ``agent_mcp/migrations/versions/0025_terminal_task_guard_trigger.py``.
# SQLite's trigger grammar only accepts a string LITERAL for the RAISE
# message (no bound params / interpolation), so the marker is frozen
# in both places and matched here by substring.
GUARD_MARKER = "terminal_task_guard"


class TerminalTaskWriteBlocked(Exception):
    """The DB-level terminal-state guard trigger refused a write.

    Defense-in-depth UNDERNEATH the Python-level checks in
    ``tools/task_tools.py`` (``_TERMINAL_TASK_STATUSES`` /
    ``_is_status_transition_allowed``) and the per-task terminal check
    in ``tools/task_comments_tools.py`` / ``db/actions/task_comments_db.py``
    — it exists to catch a write path that never checked task
    terminality at all (the exact OBS-R12-2 class). Every already-
    guarded call site refuses the mutation in Python before reaching
    here, so in normal operation this is never raised.
    """

    def __init__(self, task_id: str, message: str | None = None) -> None:
        self.task_id = task_id
        super().__init__(
            message
            or (
                f"Cannot modify task '{task_id}': it is in a terminal "
                "state (completed/cancelled/failed); its "
                "status/assigned_to/priority/notes/title/description "
                "(and its task_comments) are frozen."
            )
        )
