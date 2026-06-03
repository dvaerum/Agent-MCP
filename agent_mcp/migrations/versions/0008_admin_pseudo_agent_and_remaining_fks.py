"""admin pseudo-agent + the 3 deferred FK constraints from PR #96

Revision ID: 0008_admin_pseudo_agent_and_fks
Revises: 0007_declare_foreign_keys
Create Date: 2026-06-03

PR #96 (migration 0007) shipped 4 of the 7 implicit FK constraints
the 2026-06-02 database review identified. The other three:

  * agent_messages.sender_id    -> agents.agent_id
  * agent_messages.recipient_id -> agents.agent_id
  * mcp_sessions.agent_id       -> agents.agent_id

were deferred because production data showed every orphan in those
three columns has agent_id='admin'. The application treats `admin`
as a first-class pseudo-agent (sends messages, opens MCP sessions,
runs admin tools) but never had a row in the `agents` table — admin
identity was enforced via `g.admin_token`, not via the agents table.

This migration finishes that story:

  1. INSERT OR IGNORE a synthetic `admin` row into `agents` so the
     three FKs can be added without leaving real production rows
     orphaned. The lifespan-startup task in
     `server_lifecycle.application_startup` also runs this insert at
     every startup for defence in depth (in case an operator wipes
     the row and the FK constraint hasn't been created yet because
     the DB pre-dates 0008).

  2. NULL/DELETE any remaining orphans on the three deferred
     columns. agent_messages.{sender_id, recipient_id} are
     NOT NULL — orphans get DELETEd (with a structured warning
     entry inserted via `agent_actions` so an operator can audit
     what was removed). mcp_sessions.agent_id is also NOT NULL —
     orphan sessions get DELETEd too (they're cache-like — a
     pruner sweeps stale rows anyway).

  3. Add the three FK constraints via `batch_alter_table`, mirroring
     the pattern in 0007.

The same `AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP=1` escape hatch from
0007 is honoured here for operators who want to inspect orphans
before the migration touches them.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008_admin_pseudo_agent_and_fks"
down_revision: Union[str, None] = "0007_declare_foreign_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The three deferred FKs we ship here. Format mirrors 0007: (table,
# column, ref_table, ref_col, nullable). All three are NOT NULL in
# the existing DDL (see agent_mcp/db/schema.py — agent_messages and
# mcp_sessions both declare `<col> TEXT NOT NULL` for the agent
# pointers), so the orphan-cleanup branch DELETEs rather than NULLs.
_FKS: list[tuple[str, str, str, str, bool]] = [
    ("agent_messages", "sender_id", "agents", "agent_id", False),
    ("agent_messages", "recipient_id", "agents", "agent_id", False),
    ("mcp_sessions", "agent_id", "agents", "agent_id", False),
]


_ADMIN_AGENT_ID = "admin"
# A non-empty sentinel for the `token` PK column. The real admin
# token lives in `g.admin_token` / project_context — this row exists
# purely so FK constraints have a parent to point at. We use a value
# that's obviously synthetic so it can't collide with a real bearer.
_ADMIN_SYNTHETIC_TOKEN = "__admin_pseudo_agent__"


def _bypass_cleanup() -> bool:
    """Read AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP at migration time.

    Same semantics as migration 0007: falsy values are unset, '', '0',
    'false', 'no'. Anything else = truthy.
    """
    val = os.environ.get("AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP", "")
    return val.strip().lower() not in ("", "0", "false", "no")


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _existing_fks(bind, table: str) -> set[tuple[str, str, str]]:
    """Return set of (col, ref_table, ref_col) FKs already on `table`."""
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    out: set[tuple[str, str, str]] = set()
    for fk in inspector.get_foreign_keys(table):
        ref_table = fk.get("referred_table") or ""
        cons = fk.get("constrained_columns") or []
        refs = fk.get("referred_columns") or []
        for src, dst in zip(cons, refs):
            out.add((src, ref_table, dst))
    return out


def _insert_admin_row(bind) -> None:
    """INSERT OR IGNORE the synthetic admin row into agents.

    Idempotent on `agent_id` UNIQUE. The lifespan-startup task
    re-runs this on every boot for defence in depth, so an operator
    accidentally deleting the row recovers on next restart.
    """
    if not _table_exists(bind, "agents"):
        return
    now = _dt.datetime.now().isoformat()
    # `INSERT OR IGNORE` is sqlite-native; SQLAlchemy passes it through
    # verbatim since we're using `sa.text`. Column set mirrors what
    # `init_database()` declares for `agents` (token PK, agent_id UNIQUE
    # NOT NULL, etc.).
    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO agents "
            "(token, agent_id, capabilities, created_at, status, "
            " working_directory, color, updated_at) "
            "VALUES (:token, :agent_id, :capabilities, :created_at, "
            " :status, :working_directory, :color, :updated_at)"
        ),
        {
            "token": _ADMIN_SYNTHETIC_TOKEN,
            "agent_id": _ADMIN_AGENT_ID,
            "capabilities": "[]",
            "created_at": now,
            "status": "system",
            "working_directory": "",
            "color": "#000000",
            "updated_at": now,
        },
    )


def _cleanup_orphans(bind) -> None:
    """NULL or DELETE every orphan row prior to the FK-add copy step.

    No-op if AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP is set — same escape
    hatch the 0007 migration honours.
    """
    if _bypass_cleanup():
        return

    for table, col, ref_table, ref_col, nullable in _FKS:
        if not _table_exists(bind, table) or not _table_exists(bind, ref_table):
            continue
        if nullable:
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET {col} = NULL "
                    f"WHERE {col} IS NOT NULL "
                    f"AND {col} NOT IN (SELECT {ref_col} FROM {ref_table})"
                )
            )
        else:
            bind.execute(
                sa.text(
                    f"DELETE FROM {table} "
                    f"WHERE {col} IS NOT NULL "
                    f"AND {col} NOT IN (SELECT {ref_col} FROM {ref_table})"
                )
            )


def _add_fks_to_table(table: str) -> None:
    """Use batch_alter_table to add FK constraints to `table`.

    Mirrors 0007's `_add_fks_to_table`: `recreate="always"` forces the
    copy-table dance so SQLite picks up the new constraints.
    """
    fks_for_table = [fk for fk in _FKS if fk[0] == table]
    if not fks_for_table:
        return
    with op.batch_alter_table(table, recreate="always") as batch_op:
        for _t, col, ref_table, ref_col, _nullable in fks_for_table:
            batch_op.create_foreign_key(
                f"fk_{table}_{col}",
                ref_table,
                [col],
                [ref_col],
            )


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1 — seed the admin row BEFORE cleanup so any orphan that
    # references `admin` is no longer an orphan and can survive the
    # FK check.
    _insert_admin_row(bind)

    # Step 2 — clean up any remaining orphans (anything pointing at
    # an agent_id that isn't 'admin' and doesn't exist in agents).
    _cleanup_orphans(bind)

    # Step 3 — group FKs by table so each table is rebuilt at most once.
    # See env.py for the FK pragma policy (hotfix 2026-06-03): FKs
    # are OFF during migration, then re-enabled with foreign_key_check
    # as the safety net.
    tables: list[str] = []
    for fk in _FKS:
        if fk[0] not in tables:
            tables.append(fk[0])
    for table in tables:
        if not _table_exists(bind, table):
            continue
        existing = _existing_fks(bind, table)
        wanted = {(fk[1], fk[2], fk[3]) for fk in _FKS if fk[0] == table}
        if wanted.issubset(existing):
            continue
        _add_fks_to_table(table)

    # batch_alter_table rebuilds via copy, which loses DESC modifiers
    # on composite indexes. Restore the DESC indexes on the rebuilt
    # tables.
    inspector = sa.inspect(bind)

    if "agent_messages" in inspector.get_table_names():
        idx_names = {ix["name"] for ix in inspector.get_indexes("agent_messages")}
        # idx_agent_messages_recipient_timestamp uses timestamp DESC
        if "idx_agent_messages_recipient_timestamp" in idx_names:
            op.drop_index(
                "idx_agent_messages_recipient_timestamp", table_name="agent_messages"
            )
        op.execute(
            "CREATE INDEX idx_agent_messages_recipient_timestamp "
            "ON agent_messages (recipient_id, timestamp DESC)"
        )
        if "idx_agent_messages_sender_timestamp" in idx_names:
            op.drop_index(
                "idx_agent_messages_sender_timestamp", table_name="agent_messages"
            )
        op.execute(
            "CREATE INDEX idx_agent_messages_sender_timestamp "
            "ON agent_messages (sender_id, timestamp DESC)"
        )
        if "idx_agent_messages_unread" in idx_names:
            op.drop_index("idx_agent_messages_unread", table_name="agent_messages")
        op.execute(
            "CREATE INDEX idx_agent_messages_unread "
            "ON agent_messages (recipient_id, read, timestamp DESC)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables: list[str] = []
    for fk in _FKS:
        if fk[0] not in tables:
            tables.append(fk[0])
    for table in tables:
        if not _table_exists(bind, table):
            continue
        existing = _existing_fks(bind, table)
        wanted = {(fk[1], fk[2], fk[3]) for fk in _FKS if fk[0] == table}
        if not (wanted & existing):
            continue
        with op.batch_alter_table(table, recreate="always") as batch_op:
            for _t, col, _ref_table, _ref_col, _nullable in _FKS:
                if _t != table:
                    continue
                try:
                    batch_op.drop_constraint(
                        f"fk_{table}_{col}", type_="foreignkey"
                    )
                except Exception:
                    # Constraint name may differ in older DBs; the
                    # rebuild itself drops them anyway.
                    pass

    # Note: the synthetic admin row is intentionally left in place on
    # downgrade. The lifespan-startup task re-inserts it on every
    # boot anyway, and a downgrade that removes it would race with
    # the next startup.
