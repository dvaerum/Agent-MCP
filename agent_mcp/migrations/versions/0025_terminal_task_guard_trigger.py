"""DB-level terminal-state guard triggers (structural backstop)

Revision ID: 0025_terminal_task_guard_trigger
Revises: 0024_drop_config_aoe_settings
Create Date: 2026-08-22

OBS-R12-2: "terminal-state carve-out miss" is a recurring bug class — a
task write path forgets that a completed/cancelled/failed task is a
frozen sink, because the invariant
(``task_tools._TERMINAL_TASK_STATUSES`` / ``_is_status_transition_
allowed``) was enforced opt-in, per call-site, in Python. Within the
current pentest ledger alone it recurred THREE times: BL-R25-1 →
``reassign``; R12-F4/R12-F5 → ``update_priority`` / ``add_note`` / the
``assigned_to``-clearing branch (all on ``tools/task_tools.py``, the
legacy ``tasks.notes`` JSON-column surface); and a live round-13
pentest sweep found a third sibling on a DIFFERENT table entirely —
the newer `task_notes` side table (db-review PR-H) that backs
``add_task_note`` / ``edit_task_note`` never checked task terminality
at all. The pre-fresh-start ledger history references five more
(BL-R16-1, BL-R16-2, BL-R17-1, BL-R17-2, BL-R18-1). This mirrors how
OBS-R11-1 closed the stale-Principal TOCTOU class by consolidating
Python-level enforcement into one structural helper — this goes one
layer lower: DB triggers the write itself can't bypass, so no future
(or, for ``task_notes``, current) write path can forget the check even
if it never heard of the Python-level convention.

## Mechanism: TRIGGER, not CHECK

SQLite table-level ``CHECK`` constraints can't reference a row's OLD
values during an UPDATE — a ``CHECK`` only ever sees the proposed NEW
row. Comparing OLD vs NEW requires either a full table rebuild (a
``CHECK`` against a shadow column) or a ``BEFORE ... FOR EACH ROW``
trigger. A trigger is a plain additive ``CREATE TRIGGER`` — no rebuild
of years of existing task data needed — and composes cleanly with the
existing single-root partial index (migration 0023).

## Trigger 1 — ``tasks`` (the terminal-sink invariant itself)

    CREATE TRIGGER trg_tasks_terminal_state_guard
    BEFORE UPDATE ON tasks FOR EACH ROW
    WHEN OLD.status IN ('completed','cancelled','failed')
      AND ( ...a protected field is actually CHANGING... )
    BEGIN SELECT RAISE(ABORT, '<marker>: ...'); END;

Protected fields — exactly what ``task_tools._update_single_task``
already freezes on a terminal task (its module docstring: "a
completed/cancelled/failed task ... refuses EVERY admin-field edit ...
(title, description, priority, notes, reassign)"), plus ``status``
itself (an absolute sink per ``_is_status_transition_allowed`` — even
a same-value terminal rewrite is refused, since re-writing it would
re-fire completion side effects at the Python layer):

    status, priority, notes, title, description

``assigned_to`` gets a NARROWER carve-out: the guard only fires when
the NEW value is being set to a live, non-NULL agent (a REASSIGN /
resurrect — the actual BL-R25-1 danger: someone else re-executing
"finished" work). Clearing ``assigned_to`` to NULL on a terminal task
is deliberately left unguarded at the DB layer, because
``tools/admin_tools.py``'s agent-purge path (BL-R17-2) has a
legitimate, already-audited reason to do exactly that: purge is a HARD
delete of the ``agents`` row, ``tasks.assigned_to`` is an FK to it, so
every task (including terminal ones) pointing at the purged agent MUST
have that FK nulled before the ``DELETE FROM agents`` runs, or the
delete trips the FK constraint. That null-out keeps the terminal
STATUS unchanged (no resurrection, no notify) — it is exactly the kind
of system-driven FK-consistency write ``_update_single_task``'s
``system_transition`` flag exists to let cross the caller-facing
ownership gate, just for a different invariant. A DB trigger can't
distinguish "system purge nulling an FK" from "caller nulling an
attribution" by intent, so the guard is scoped to the strictly more
dangerous half (reassign-to-someone) rather than block the legitimate
purge and leave a dangling FK.

``child_tasks``, ``depends_on_tasks``, ``parent_task`` stay fully
mutable on a terminal row — two EXISTING, legitimate write paths rely
on this: ``task_tools._link_child_to_parent`` appends to a PARENT's
``child_tasks`` mirror when a new child task is created (a completed
parent has never blocked new children referencing it), and
``delete_task_tool_impl``'s cascade reconciles ``child_tasks`` /
``depends_on_tasks`` on OTHER rows (parent + dependents) when a task
is deleted — those rows are routinely terminal and the reference
cleanup must still land or the JSON mirror goes stale. Guarding on
VALUE CHANGE (``IS NOT``, not column presence) means a write that only
touches these three fields (+ ``updated_at``) on an already-terminal
row sails through untouched.

## Triggers 2 + 3 — ``task_notes`` (round-13 class-sweep addition)

The side table has no ``status`` column of its own — terminality is a
property of the PARENT task, joined via ``task_id``. Two triggers
(INSERT, UPDATE) join back to ``tasks.status`` in their ``WHEN``
clause:

    WHEN (SELECT status FROM tasks WHERE task_id = NEW.task_id)
         IN ('completed','cancelled','failed')

A ``task_id`` with no matching ``tasks`` row (orphan note, or the
add-note ownership gate in ``task_notes_tools.py`` letting a phantom
through) makes the subquery return NULL; ``NULL IN (...)`` is NULL
(not true), so the WHEN clause is false and the write proceeds — this
guard only ever blocks a note whose parent task provably exists AND is
terminal, never a nonexistent-task edge case (that's ``NotFound``'s
job in the tool layer, unchanged).

DELETE is deliberately NOT guarded — same reasoning as ``tasks`` itself
(structural DELETE is a different operation from content mutation) and
concretely needed so a future fix for the known task_notes-orphan-row
gap (deleting a task today never cascade-deletes its notes) can
``DELETE FROM task_notes WHERE task_id = ?`` even when the task being
deleted is terminal, without this guard fighting that cleanup.

## Error surface

``RAISE(ABORT, ...)`` rolls back only the offending statement (not the
whole transaction) and raises ``sqlite3.IntegrityError``. The message
is a static marker string (SQLite's trigger grammar requires a string
literal for ``RAISE``'s message, not an interpolated expression) —
``agent_mcp.db.terminal_task_guard.GUARD_MARKER`` is matched by both
writer layers (``repositories.task_repository`` for ``tasks``,
``db.actions.task_notes_db`` for ``task_notes``) and re-raised as the
typed ``TerminalTaskWriteBlocked``, which the tool layer translates
into a clean ``Conflict`` (409) response — never a raw 500 leaking the
trigger's SQL text.

## Fresh vs legacy DBs

Triggers are not SQLAlchemy-modellable (no ORM equivalent), so unlike
the 0023 index this migration is the ONLY place they are created — it
runs (and is needed) on both a fresh ``create_all()`` DB and a legacy
one; there is nothing for the ORM's ``create_all()`` to already have
created.

## Downgrade

Drops all three triggers.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from agent_mcp.db.terminal_task_guard import GUARD_MARKER

# revision identifiers, used by Alembic.
revision: str = "0025_terminal_task_guard_trigger"
down_revision: Union[str, None] = "0024_drop_config_aoe_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TASKS_TRIGGER = "trg_tasks_terminal_state_guard"
_NOTES_INSERT_TRIGGER = "trg_task_notes_terminal_guard_insert"
_NOTES_UPDATE_TRIGGER = "trg_task_notes_terminal_guard_update"

_TASKS_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_TASKS_TRIGGER}
BEFORE UPDATE ON tasks
FOR EACH ROW
WHEN OLD.status IN ('completed', 'cancelled', 'failed')
  AND (
    NEW.status IS NOT OLD.status
    OR NEW.priority IS NOT OLD.priority
    OR NEW.notes IS NOT OLD.notes
    OR NEW.title IS NOT OLD.title
    OR NEW.description IS NOT OLD.description
    OR (NEW.assigned_to IS NOT OLD.assigned_to AND NEW.assigned_to IS NOT NULL)
  )
BEGIN
  SELECT RAISE(ABORT, '{GUARD_MARKER}: task is in a terminal state (completed/cancelled/failed); status/priority/notes/title/description are frozen and assigned_to may only be cleared, never reassigned');
END;
"""

_NOTES_INSERT_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_NOTES_INSERT_TRIGGER}
BEFORE INSERT ON task_notes
FOR EACH ROW
WHEN (SELECT status FROM tasks WHERE task_id = NEW.task_id) IN ('completed', 'cancelled', 'failed')
BEGIN
  SELECT RAISE(ABORT, '{GUARD_MARKER}: cannot add a task_note; parent task is in a terminal state (completed/cancelled/failed)');
END;
"""

_NOTES_UPDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_NOTES_UPDATE_TRIGGER}
BEFORE UPDATE ON task_notes
FOR EACH ROW
WHEN (SELECT status FROM tasks WHERE task_id = OLD.task_id) IN ('completed', 'cancelled', 'failed')
BEGIN
  SELECT RAISE(ABORT, '{GUARD_MARKER}: cannot edit a task_note; parent task is in a terminal state (completed/cancelled/failed)');
END;
"""


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "tasks"):
        bind.execute(sa.text(_TASKS_SQL))
    if _table_exists(bind, "task_notes"):
        bind.execute(sa.text(_NOTES_INSERT_SQL))
        bind.execute(sa.text(_NOTES_UPDATE_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TASKS_TRIGGER}"))
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {_NOTES_INSERT_TRIGGER}"))
    bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {_NOTES_UPDATE_TRIGGER}"))
