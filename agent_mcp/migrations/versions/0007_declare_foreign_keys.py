"""declare 4 of the 7 implicit FK constraints from the 2026-06-02 review

Revision ID: 0007_declare_foreign_keys
Revises: 0006_db_review_indexes
Create Date: 2026-06-03

The 2026-06-02 database review identified that `PRAGMA foreign_keys=ON`
is set per connection but **no FK constraints are declared in DDL** —
so the pragma is a no-op. The review listed seven implicit
relationships; this migration ships **four** of them:

  * agents.current_task             -> tasks.task_id            ✓
  * tasks.parent_task               -> tasks.task_id            ✓
  * tasks.assigned_to               -> agents.agent_id          ✓
  * claude_code_sessions.agent_id   -> agents.agent_id          ✓

The other three are deferred to a follow-up PR:

  * agent_messages.sender_id        -> agents.agent_id          ✗
  * agent_messages.recipient_id     -> agents.agent_id          ✗
  * mcp_sessions.agent_id           -> agents.agent_id          ✗

Reason: production data analysis of washing-brothers shows every
orphan in these three columns has agent_id='admin'. The application
treats 'admin' as a first-class pseudo-agent (sends messages, opens
MCP sessions, runs admin tools) but no `agents` row is ever inserted
for it — admin identity is enforced via `g.admin_token`, not via the
agents table. Adding the FK without first seeding an 'admin' agents
row would break admin sessions on every startup.

The right follow-up is to seed an 'admin' synthetic row at lifespan
startup, then re-add the deferred FKs. That work touches the agent
restore/purge cascade in `feat/agent-restore-and-purge`, so it's
intentionally split off rather than bundled here.

## SQLite's "no ALTER ADD CONSTRAINT" problem

SQLite's `ALTER TABLE` cannot add FK constraints to existing tables.
The standard workaround — and the one Alembic implements transparently
in `batch_alter_table` — is to:

  1. Create a new table with the desired DDL (including FKs).
  2. Copy all rows from the old table.
  3. Drop the old table; rename the new one.

Critically, step 2 happens **with `foreign_keys` ON in the connection**
(that's the runtime default in agent-mcp, and `env.py` now mirrors it
for the migration's own connection). If any row references a
non-existent parent, the copy fails with `FOREIGN KEY constraint
failed`.

## Orphan cleanup

Production DBs may contain orphans accumulated before this PR
(deleted-agent leftovers, stale parent_task pointers, etc.). The
2026-06-02 orphan probe against the washing-brothers DB found:
  * agents.current_task: 2 orphans
  * tasks.parent_task: 10 orphans

For each nullable FK column (all four of the FKs in this migration),
orphan rows have the offending column UPDATEd to NULL — the row stays,
only the dangling pointer is cleared. No DELETEs in this migration.

## Bypass escape hatch

Operators who want to inspect orphans before the migration touches
them can set `AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP=1`. With the flag,
no rows are mutated; the FK constraint creation that follows will
fail loudly (non-zero exit, `FOREIGN KEY constraint failed`), which
is the intended signal — fix the data, unset the flag, retry.

## ON DELETE behavior

`ON DELETE` is intentionally left at the SQLite default (`NO ACTION`)
rather than `CASCADE` or `SET NULL`. The application owns lifecycle:
agent deletion already cascades through Python code (purge tools);
adding implicit cascade at the DDL layer would surprise existing
tools, especially around the soft-delete / restore flow in
`feat/agent-restore-and-purge`. Same reasoning for `ON UPDATE`.
"""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007_declare_foreign_keys"
down_revision: Union[str, None] = "0006_db_review_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, ref_table, ref_col, nullable). Nullable controls
# whether orphans get UPDATE-to-NULL (nullable) or DELETE (NOT NULL).
# All four FKs we ship are nullable; DELETE paths are kept in
# `_cleanup_orphans` for the deferred (admin pseudo-agent) follow-up.
_FKS: list[tuple[str, str, str, str, bool]] = [
    ("agents", "current_task", "tasks", "task_id", True),
    ("tasks", "parent_task", "tasks", "task_id", True),
    ("tasks", "assigned_to", "agents", "agent_id", True),
    ("claude_code_sessions", "agent_id", "agents", "agent_id", True),
]


def _bypass_cleanup() -> bool:
    """Read AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP at migration time.

    Falsy values: unset, '', '0', 'false', 'no'.
    Anything else = truthy.
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


def _cleanup_orphans(bind) -> None:
    """NULL or DELETE every orphan row prior to the FK-add copy step.

    No-op if AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP is set.
    """
    if _bypass_cleanup():
        # Operator opted out — leave orphans in place; the
        # batch_alter_table copy below will fail with FOREIGN KEY
        # constraint failed and a non-zero exit. That's the intended
        # signal.
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

    `recreate="always"` forces the copy-table dance so SQLite picks up
    the new constraints; Alembic infers naming from the
    `naming_convention` set in `migrations/env.py`'s
    target_metadata, which is enough for SQLite's introspection.
    """
    fks_for_table = [fk for fk in _FKS if fk[0] == table]
    if not fks_for_table:
        return
    with op.batch_alter_table(table, recreate="always") as batch_op:
        for _t, col, ref_table, ref_col, _nullable in fks_for_table:
            # constraint name is per-(table,col); SQLite ignores the
            # name on introspection but Alembic requires one.
            batch_op.create_foreign_key(
                f"fk_{table}_{col}",
                ref_table,
                [col],
                [ref_col],
            )


def upgrade() -> None:
    bind = op.get_bind()
    _cleanup_orphans(bind)

    # NOTE: env.py turns `foreign_keys=OFF` for the migration
    # connection (hotfix 2026-06-03) so the batch_alter_table
    # rebuild dance can DROP child tables without the previously-
    # added FK blocking it. env.py re-enables FKs and runs
    # `PRAGMA foreign_key_check` after all migrations complete.
    # Earlier versions ran with FKs ON and broke on live washing-
    # brothers-style DBs (CI's pristine schemas masked the bug).
    # Orphan cleanup above ensures the data is FK-clean before the
    # rebuild, so the post-migration foreign_key_check passes.

    # Group FKs by table so each table is rebuilt at most once.
    tables = []
    for fk in _FKS:
        if fk[0] not in tables:
            tables.append(fk[0])
    for table in tables:
        if not _table_exists(bind, table):
            continue
        # Skip if all FKs for this table are already present (idempotency).
        existing = _existing_fks(bind, table)
        wanted = {(fk[1], fk[2], fk[3]) for fk in _FKS if fk[0] == table}
        if wanted.issubset(existing):
            continue
        _add_fks_to_table(table)

    # batch_alter_table rebuilds the table via copy, which loses the
    # DESC modifier on any composite index it re-creates from
    # reflection. The 2026-06-02 review's critical composite was
    # `(assigned_to, updated_at DESC)`; restore that exact DDL here.
    # Same rationale for any other DESC-sorted index this migration
    # might disturb.
    inspector = sa.inspect(bind)
    if "tasks" in inspector.get_table_names():
        idx_names = {ix["name"] for ix in inspector.get_indexes("tasks")}
        if "idx_tasks_assigned_to_updated_at" in idx_names:
            op.drop_index(
                "idx_tasks_assigned_to_updated_at", table_name="tasks"
            )
        op.execute(
            "CREATE INDEX idx_tasks_assigned_to_updated_at "
            "ON tasks (assigned_to, updated_at DESC)"
        )


def downgrade() -> None:
    # Re-create each affected table without the FK constraints.
    # batch_alter_table with recreate="always" and no explicit FK
    # operations strips them — but we need to pass a no-op to avoid
    # the "no operations" warning.
    bind = op.get_bind()
    tables = []
    for fk in _FKS:
        if fk[0] not in tables:
            tables.append(fk[0])
    for table in tables:
        if not _table_exists(bind, table):
            continue
        existing = _existing_fks(bind, table)
        wanted = {(fk[1], fk[2], fk[3]) for fk in _FKS if fk[0] == table}
        if not (wanted & existing):
            # Nothing of ours to remove.
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
                    # The constraint name may differ in older DBs;
                    # recreate="always" without explicit ops will still
                    # rebuild the table from the metadata reflection,
                    # which by this point doesn't include the FKs.
                    pass
