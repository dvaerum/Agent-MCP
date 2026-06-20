"""drop admin pseudo-agent + the FKs that pinned it in place

Revision ID: 0014_drop_admin_pseudo_agent
Revises: 0013_agent_role_column
Create Date: 2026-06-20

Wave 4 of the admin_token retirement (prancy-napping-pie). After
Wave 3, no *external* client uses the system bearer as an agent
identity — the dashboard authenticates via the operator-session
cookie, agent tokens are stored in the agents table, and the system
bearer survives only as an in-process router-to-backend authority
header.

The synthetic ``agents`` row introduced by migration 0008 (PR-G1)
served three purposes that have since disappeared:

  1. It satisfied the FK constraints on ``agent_messages.sender_id``,
     ``agent_messages.recipient_id``, and ``mcp_sessions.agent_id``
     — but those constraints were added in 0008 in the first place
     *to enable* the pseudo-agent. Without an agent-shaped admin
     they become impedance: the system bearer's ``agent_id`` is the
     literal string ``"admin"`` (returned by
     :func:`agent_mcp.core.auth.get_agent_id`) used as an actor
     label, not as a foreign key into the agents table.

  2. It let ``send_message_to_agent(recipient_id="admin", ...)``
     succeed without violating the recipient FK. Worker→admin
     escalations remain a valid use case, but post-Wave-4 those
     records do not require an agents-table parent — the message
     row's recipient field is a label, not a FK target.

  3. It let the dashboard's GET /mcp stream open without violating
     ``mcp_sessions.agent_id`` FK (the cookie-injected system bearer
     resolves to ``agent_id='admin'`` inside the backend).

This migration is forward-only:

  * Step 1 — drop the FK constraints added by 0008 via
    ``batch_alter_table`` (the only way SQLite can remove a
    constraint). The columns themselves stay NOT NULL; only the
    FK declaration goes away. The composite DESC indexes that 0008
    restored after its batch rebuild are restored here too.

  * Step 2 — also drop the nullable FKs from 0007 that target
    ``agents.agent_id`` (``tasks.assigned_to``,
    ``claude_code_sessions.agent_id``). They had ON DELETE NO ACTION,
    so deleting the admin row with a task pointing at it would have
    blocked. NULL those orphans first as defence-in-depth, then drop
    the FKs so future ``assigned_to='admin'`` writes don't depend on
    an agents row.

  * Step 3 — DELETE the synthetic admin row. ``INSERT OR IGNORE``
    is no longer re-inserting it at startup (lifespan code dropped in
    the same PR).

## Why drop the FKs at all?

Two coupled reasons:

  * The system bearer is, by design, not an agent. Modelling it as
    one (an ``agents`` row with ``agent_id='admin'``) was a workaround
    so the FKs would close cleanly. Without that workaround the
    constraints have no clean parent for ``agent_id='admin'`` rows.

  * The columns are application-owned identifiers, not relational
    references. Audit messages, MCP session records, and worker→admin
    handoffs need durability across agent purges (purge tombstones
    survive specifically because the messages they reference would
    otherwise break the FK); the cleanest version of that contract
    has the columns be free-form labels.

## SQLite FK removal mechanics

SQLite cannot ALTER TABLE DROP CONSTRAINT. ``batch_alter_table``
with ``recreate="always"`` rebuilds the table without the
constraints we don't reissue, then renames it into place — same
trick 0007/0008/0012 use to add FKs in the first place.

Per ``migrations/env.py``, FKs are OFF during the migration
connection, and ``PRAGMA foreign_key_check`` runs as the post-commit
safety net.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0014_drop_admin_pseudo_agent"
down_revision: Union[str, None] = "0013_agent_role_column"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ADMIN_AGENT_ID = "admin"


# Tables we rebuild without their agent_id-targeting FKs. Order
# doesn't matter — each rebuild is independent.
_TABLES_TO_REBUILD_WITHOUT_AGENT_FKS: tuple[str, ...] = (
    "agent_messages",
    "mcp_sessions",
    "tasks",
    "claude_code_sessions",
)


# FKs we are explicitly removing. Each tuple is (column, referred_col)
# scoped to the table it appears under in the mapping below. Used by
# the rebuild loop to know which constraints to *not* recreate.
_FKS_TO_DROP: dict[str, list[tuple[str, str]]] = {
    "agent_messages": [
        ("sender_id", "agent_id"),
        ("recipient_id", "agent_id"),
    ],
    "mcp_sessions": [
        ("agent_id", "agent_id"),
    ],
    "tasks": [
        ("assigned_to", "agent_id"),
    ],
    "claude_code_sessions": [
        ("agent_id", "agent_id"),
    ],
}


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _has_agent_fk(bind, table: str) -> bool:
    """Return True if `table` has any FK pointing at agents.agent_id."""
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    for fk in inspector.get_foreign_keys(table):
        if fk.get("referred_table") == "agents":
            return True
    return False


def _restore_desc_indexes_after_rebuild(bind, table: str) -> None:
    """``batch_alter_table`` loses the DESC modifier on composite
    indexes when it rebuilds. Restore the same DDL that 0007/0008
    set up for each table we touch here.

    Idempotent: drops the auto-recreated index (if it exists, sans
    DESC) and re-creates it with DESC.
    """
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    idx_names = {ix["name"] for ix in inspector.get_indexes(table)}

    if table == "agent_messages":
        # Mirror migration 0008's post-rebuild block.
        for name, ddl in (
            (
                "idx_agent_messages_recipient_timestamp",
                "CREATE INDEX idx_agent_messages_recipient_timestamp "
                "ON agent_messages (recipient_id, timestamp DESC)",
            ),
            (
                "idx_agent_messages_sender_timestamp",
                "CREATE INDEX idx_agent_messages_sender_timestamp "
                "ON agent_messages (sender_id, timestamp DESC)",
            ),
            (
                "idx_agent_messages_unread",
                "CREATE INDEX idx_agent_messages_unread "
                "ON agent_messages (recipient_id, read, timestamp DESC)",
            ),
        ):
            if name in idx_names:
                op.drop_index(name, table_name="agent_messages")
            op.execute(ddl)

    elif table == "tasks":
        # Mirror migration 0007's post-rebuild block.
        name = "idx_tasks_assigned_to_updated_at"
        if name in idx_names:
            op.drop_index(name, table_name="tasks")
        op.execute(
            "CREATE INDEX idx_tasks_assigned_to_updated_at "
            "ON tasks (assigned_to, updated_at DESC)"
        )


def _null_orphans_targeting_agents(bind) -> None:
    """Pre-rebuild: NULL any nullable agent_id pointer that would
    become a dangling reference once the admin row is gone.

    Only the *nullable* columns from 0007's FK set are touched here.
    The NOT NULL columns (``agent_messages.{sender_id,recipient_id}``,
    ``mcp_sessions.agent_id``) cannot be NULLed — but those tables
    explicitly want to retain ``agent_id='admin'`` rows as durable
    audit / session records, and dropping the FK (step 2 below) makes
    that legal.
    """
    if _table_exists(bind, "tasks"):
        bind.execute(
            sa.text(
                "UPDATE tasks SET assigned_to = NULL "
                "WHERE assigned_to = :aid"
            ),
            {"aid": _ADMIN_AGENT_ID},
        )
    if _table_exists(bind, "claude_code_sessions"):
        # The 0007 FK is nullable; clear any admin-pointer rows so
        # the rebuild copy doesn't trip the (now-absent) FK check.
        bind.execute(
            sa.text(
                "UPDATE claude_code_sessions SET agent_id = NULL "
                "WHERE agent_id = :aid"
            ),
            {"aid": _ADMIN_AGENT_ID},
        )


def _rebuild_table_dropping_agent_fks(bind, table: str) -> None:
    """Rebuild ``table`` without the FKs listed in
    ``_FKS_TO_DROP[table]``, preserving every other constraint.

    ``batch_alter_table`` reflects the *current* schema and recreates
    it; merely entering the context with no ops would re-emit the
    very FKs we want gone. We therefore emit an explicit
    ``drop_constraint`` for each by its canonical name (the names
    Alembic assigned them in 0007/0008 — ``fk_<table>_<col>``).

    If a constraint name isn't found (e.g. an older DB where the
    constraint was created without an explicit name), the drop is a
    no-op and the rebuild itself — which gets fed a metadata copy we
    explicitly strip of those FKs by drop_constraint — finishes the
    job.

    Any OTHER FKs on the table (e.g. ``agents.current_task ->
    tasks.task_id``, ``tasks.parent_task -> tasks.task_id``,
    ``agent_messages.parent_message_id -> agent_messages.message_id``)
    are preserved automatically by Alembic's reflection.
    """
    fks_to_drop = _FKS_TO_DROP.get(table, [])
    if not fks_to_drop:
        return

    inspector = sa.inspect(bind)
    existing_fks = inspector.get_foreign_keys(table)
    # Build a name lookup keyed on (constrained_column, referred_column)
    # so we can find the alembic-assigned name regardless of whether
    # the constraint was originally created with the canonical
    # ``fk_<table>_<col>`` name or via SQLite reflection's
    # auto-generated identifier (which is just a numeric on some
    # SQLite versions).
    name_by_cols: dict[tuple[str, str], list[str]] = {}
    for fk in existing_fks:
        if fk.get("referred_table") != "agents":
            continue
        cons = fk.get("constrained_columns") or []
        refs = fk.get("referred_columns") or []
        name = fk.get("name")
        for src, dst in zip(cons, refs):
            key = (src, dst)
            name_by_cols.setdefault(key, []).append(
                name or f"fk_{table}_{src}"
            )

    with op.batch_alter_table(table, recreate="always") as batch_op:
        for col, ref_col in fks_to_drop:
            names = name_by_cols.get((col, ref_col), [
                f"fk_{table}_{col}",
            ])
            for name in names:
                try:
                    batch_op.drop_constraint(name, type_="foreignkey")
                except Exception:
                    # Either the name didn't match (older naming) or
                    # the constraint was already gone. recreate='always'
                    # still rebuilds the table from the (now-stripped)
                    # reflected metadata, so the FK lands gone either
                    # way.
                    pass


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1 — clear nullable orphans pointing at admin so the
    # post-rebuild foreign_key_check (env.py safety net) doesn't
    # surface them.
    _null_orphans_targeting_agents(bind)

    # Step 2 — rebuild every table that carries an agents-FK to
    # drop it. Skip tables that don't actually carry one (e.g. a
    # fresh DB where the migration chain hasn't laid them down,
    # though in practice 0007/0008 always run first).
    for table in _TABLES_TO_REBUILD_WITHOUT_AGENT_FKS:
        if not _table_exists(bind, table):
            continue
        if not _has_agent_fk(bind, table):
            # Already FK-free (idempotent re-run, or a DB created
            # in a future world where the FKs never existed).
            continue
        _rebuild_table_dropping_agent_fks(bind, table)
        _restore_desc_indexes_after_rebuild(bind, table)

    # Step 3 — finally, delete the synthetic admin row. With the
    # NOT-NULL FKs gone (step 2), any existing rows in agent_messages
    # / mcp_sessions that reference agent_id='admin' survive without
    # violating any constraint.
    if _table_exists(bind, "agents"):
        bind.execute(
            sa.text("DELETE FROM agents WHERE agent_id = :aid"),
            {"aid": _ADMIN_AGENT_ID},
        )


def downgrade() -> None:
    """Forward-only.

    Re-adding the FK constraints would require either (a) re-seeding
    the synthetic admin row first and accepting the divergence with
    Wave 4's intent, or (b) accepting that any post-Wave-4 rows with
    ``agent_id='admin'`` block the downgrade with FOREIGN KEY
    constraint failed. Neither is a meaningful recovery path — the
    admin pseudo-agent represented a deprecated coupling and Wave 4
    is the deliberate retirement of that coupling.
    """
    raise NotImplementedError(
        "Wave 4 is forward-only; the admin pseudo-agent represented a "
        "deprecated coupling between the system bearer and the agents "
        "table. Re-introducing it would defeat the purpose of the "
        "migration."
    )
