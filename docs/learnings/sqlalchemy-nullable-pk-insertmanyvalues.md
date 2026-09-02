# A nullable autoincrement PK breaks multi-row inserts under SQLAlchemy 2.0

Found while renaming `task_notes` → `task_comments`
(`agent_mcp/db/models/task_comment.py`, migration
`0026_rename_task_notes_to_task_comments.py`). `TaskComment.note_id` is
declared `Integer, primary_key=True, autoincrement=True, nullable=True`
— the `nullable=True` exists only to byte-match migration
`0009_task_notes_side_table.py`'s frozen raw DDL text (`note_id INTEGER
PRIMARY KEY AUTOINCREMENT`, no explicit `NOT NULL`).

That single `nullable=True` broke inserting 2+ `TaskComment` rows in
one `session.commit()`:

```
InvalidRequestError: Column task_comments.note_id has been marked as
a sentinel column with no default generation function
```

SQLAlchemy 2.0's `insertmanyvalues` optimization auto-selects an
autoincrement PK as the "sentinel" it uses to correlate `RETURNING`
rows back to their ORM objects when multiple new rows are flushed
together. It refuses to do this for a nullable column with no default
generator — single-row inserts never hit this path (no correlation
needed), so the bug is invisible until something batches.

**Why this had never fired before**: pre-rename, the ORM table name
(`task_notes`) matched migration 0009's raw table name exactly, so a
from-scratch `init_database()` always created the table via
`Base.metadata.create_all()` first, and 0009's `_table_exists` guard
skipped its own raw DDL — the nullable-PK text was dead code for the
entire life of the old model. Renaming the ORM table to `task_comments`
made migration 0026 fall back to replaying 0009's raw DDL on a real
migration-chain bootstrap (to pick up the table 0009 originally
created before renaming it), which is what first made the mismatch
load-bearing.

**Fix**: `{"implicit_returning": False}` in `__table_args__`, not
`nullable=False`. This looked backwards until we checked
`tests/test_orm_is_source_of_truth.py::test_migration_chain_matches_create_all_schema`,
which asserts a fresh `create_all()` DB and the real
migration-chain-bootstrapped DB reflect byte-identical schemas
(nullability included). Changing the ORM to `nullable=False` passes on
a fresh install but a real deployment still ends up `nullable=True`
(via 0009's frozen text) — trading an intermittent bug (2+ row
commits) for a permanent, always-on schema mismatch that test would
catch on every run. `implicit_returning=False` falls back to SQLite's
plain `lastrowid`-per-INSERT path, which needs no sentinel at all —
correct because nothing in this codebase depends on `RETURNING`
semantics for this table (verified: no `.returning(`/`RETURNING` usage
anywhere against it).

**When this recurs**: any future migration that renames an ORM table
away from an older, still-frozen migration's raw table name, where
that older migration's raw DDL text left a PK column's nullability
un-pinned. Check the new table's PK nullability in the raw DDL it's
falling back to before assuming `nullable=False` is the "obvious" fix
— it may not match what a real migration-chain deployment produces.
