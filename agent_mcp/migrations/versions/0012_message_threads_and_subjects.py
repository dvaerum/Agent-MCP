"""message threads + subjects: subject + parent_message_id FK

Revision ID: 0012_message_threads_and_subjects
Revises: 0011_orm_is_source_of_truth
Create Date: 2026-06-07

Adds the two columns + self-FK + index that turn `agent_messages`
into an email-style thread model:

  * subject              TEXT NULL   — root-only summary line.
  * parent_message_id    TEXT NULL   — self-FK to
                                       agent_messages(message_id),
                                       ON DELETE SET NULL.
  * idx_agent_messages_parent        — supports thread-by-root
                                       lookups (WHERE parent = ?).

Replies set `parent_message_id` to their root's `message_id` and
leave `subject` NULL. The application layer (route + tool handlers)
enforces "replies have NULL subject"; the DB layer just makes both
fields optional.

## ON DELETE SET NULL rationale

Deleting a root message must NOT cascade to its replies — a replies-
chain may be archived/audited independently of the root. SET NULL
keeps the reply rows but reverts them to root status (no parent), at
which point the application can re-thread or surface them as
orphans. CASCADE was rejected because message deletion is a soft-
purge in practice and we don't want collateral data loss.

## SQLite ADD COLUMN + batch_alter_table dance

Both new columns are NULLable, so `ALTER TABLE ... ADD COLUMN` works
directly without a DEFAULT (NULL is the implicit fill for existing
rows). That's idempotent on column-already-present via the
`PRAGMA table_info` gate.

Adding the self-FK is the part SQLite can't do via ALTER TABLE.
`batch_alter_table(recreate="always")` rebuilds the table with the
new constraint — same pattern as migration 0007/0008.

## Fresh-DB co-existence with the ORM

`init_database()` runs `Base.metadata.create_all()` before this
migration. The ORM model (`agent_mcp.db.models.agent_message`)
declares the two new columns + the index, so a fresh DB picks them
up via `create_all` first. This migration then runs, sees the
columns already present (idempotent ADD COLUMN), and only proceeds
to the FK rebuild — which is also idempotent on FK-already-present
via the `_existing_fks` check (mirroring 0007's pattern).

## env.py FK pragma

The FK rebuild happens with `foreign_keys=OFF` thanks to env.py's
hotfix (2026-06-03). The orphan cleanup below ensures no live row
references a non-existent parent before the rebuild — without it,
the post-migration `foreign_key_check` safety net in env.py would
raise.
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0012_message_threads_and_subjects"
down_revision: Union[str, None] = "0011_orm_is_source_of_truth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, ref_table, ref_col, on_delete). The agent_messages
# parent_message_id is self-referential — ref_table == table.
_FKS: list[tuple[str, str, str, str, str]] = [
    ("agent_messages", "parent_message_id", "agent_messages", "message_id", "SET NULL"),
]


def _bypass_cleanup() -> bool:
    """Honour the AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP escape hatch
    introduced in 0007."""
    val = os.environ.get("AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP", "")
    return val.strip().lower() not in ("", "0", "false", "no")


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _column_names(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def _index_names(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table)}


def _existing_fks(bind, table: str) -> set[tuple[str, str, str]]:
    """Return {(col, ref_table, ref_col)} of FKs already on `table`."""
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


def _cleanup_orphans(bind) -> None:
    """NULL out any parent_message_id that doesn't reference a real row.

    Since `parent_message_id` is nullable, the cleanup is non-
    destructive — orphans get reverted to root status rather than
    DELETEd. No-op under AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP.
    """
    if _bypass_cleanup():
        return

    for table, col, ref_table, ref_col, _on_delete in _FKS:
        if not _table_exists(bind, table):
            continue
        # All FKs in this migration are nullable + self-referential, so
        # the simpler NULL-out branch covers both. (Kept here as a
        # parallel to 0007 / 0008 in case a future revision adds a
        # NOT NULL self-FK that needs DELETE semantics.)
        bind.execute(
            sa.text(
                f"UPDATE {table} SET {col} = NULL "
                f"WHERE {col} IS NOT NULL "
                f"AND {col} NOT IN (SELECT {ref_col} FROM {ref_table})"
            )
        )


def _add_columns_if_missing(bind) -> None:
    """Add the two new columns to agent_messages on existing DBs.

    Idempotent on column-already-present, so this is safe on fresh
    DBs where `create_all` already created the columns from the ORM
    model.
    """
    if not _table_exists(bind, "agent_messages"):
        return
    existing = _column_names(bind, "agent_messages")
    if "subject" not in existing:
        bind.execute(sa.text("ALTER TABLE agent_messages ADD COLUMN subject TEXT"))
    if "parent_message_id" not in existing:
        bind.execute(
            sa.text(
                "ALTER TABLE agent_messages ADD COLUMN parent_message_id TEXT"
            )
        )


def _add_fks_to_table(table: str) -> None:
    """batch_alter_table with `recreate="always"` to add the self-FK."""
    fks_for_table = [fk for fk in _FKS if fk[0] == table]
    if not fks_for_table:
        return
    with op.batch_alter_table(table, recreate="always") as batch_op:
        for _t, col, ref_table, ref_col, on_delete in fks_for_table:
            batch_op.create_foreign_key(
                f"fk_{table}_{col}",
                ref_table,
                [col],
                [ref_col],
                ondelete=on_delete,
            )


def _ensure_parent_index(bind) -> None:
    """Create idx_agent_messages_parent if missing.

    `batch_alter_table` rebuild reflects existing indexes and
    re-creates them, so a fresh DB that already had the index from
    `create_all` keeps it through the rebuild. This is the catch-all
    for the pre-rebuild path and for downgrade→upgrade cycles.
    """
    if not _table_exists(bind, "agent_messages"):
        return
    idx = _index_names(bind, "agent_messages")
    if "idx_agent_messages_parent" not in idx:
        op.create_index(
            "idx_agent_messages_parent",
            "agent_messages",
            ["parent_message_id"],
        )


def _restore_desc_indexes(bind) -> None:
    """batch_alter_table rebuild loses DESC modifiers on composite
    indexes; restore them. Same maintenance fix migration 0008 does."""
    inspector = sa.inspect(bind)
    if "agent_messages" not in inspector.get_table_names():
        return
    idx_names = {ix["name"] for ix in inspector.get_indexes("agent_messages")}
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


def upgrade() -> None:
    bind = op.get_bind()

    # Step 1: add the columns on existing DBs. Idempotent on fresh
    # DBs where create_all already added them.
    _add_columns_if_missing(bind)

    # Step 2: clean up orphans before the FK rebuild so the safety-
    # net `foreign_key_check` in env.py passes.
    _cleanup_orphans(bind)

    # Step 3: rebuild agent_messages with the self-FK if not yet
    # present. Idempotent on FK-already-present.
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

    # Step 4: ensure the supporting index. Runs after the rebuild so
    # the index is created on the rebuilt table.
    _ensure_parent_index(bind)

    # Step 5: restore DESC composite indexes the rebuild may have
    # downgraded to ASC during reflection.
    _restore_desc_indexes(bind)


def downgrade() -> None:
    bind = op.get_bind()

    # Drop the parent index first so the table rebuild doesn't need
    # to re-create it.
    if _table_exists(bind, "agent_messages"):
        idx = _index_names(bind, "agent_messages")
        if "idx_agent_messages_parent" in idx:
            op.drop_index("idx_agent_messages_parent", table_name="agent_messages")

    # Rebuild without the FK.
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
            for _t, col, _ref_table, _ref_col, _on_delete in _FKS:
                if _t != table:
                    continue
                try:
                    batch_op.drop_constraint(
                        f"fk_{table}_{col}", type_="foreignkey"
                    )
                except Exception:
                    # Constraint name may differ in older DBs.
                    pass

    # Drop the columns last.
    if _table_exists(bind, "agent_messages"):
        existing = _column_names(bind, "agent_messages")
        if "parent_message_id" in existing:
            bind.execute(
                sa.text("ALTER TABLE agent_messages DROP COLUMN parent_message_id")
            )
        if "subject" in existing:
            bind.execute(sa.text("ALTER TABLE agent_messages DROP COLUMN subject"))
