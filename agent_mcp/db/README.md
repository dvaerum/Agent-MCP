# Database layer

Agent-MCP stores per-project state in a SQLite database at
`<project_dir>/.agent/mcp_state.db`. Two surfaces talk to it:

1. **Raw SQL** — `agent_mcp.db.connection.get_db_connection()` returns
   a `sqlite3.Connection`. This is the legacy path; most tools and a
   few REST handlers still use it. New code should not.
2. **SQLAlchemy ORM** — `agent_mcp.db.engine.SessionLocal()` returns a
   SQLAlchemy `Session` bound to the same DB file. Models live under
   `agent_mcp/db/models/`. Phase 7a started this migration with
   `ProjectContext`; subsequent phases (7g–7m) will migrate the
   remaining tables one at a time.

Both surfaces apply the same pragmas (`journal_mode=WAL`,
`foreign_keys=ON`), so it is safe to mix them inside the same
transaction — call `session.connection().connection.cursor()` to get
the underlying DB-API cursor when an ORM tool needs to call a raw-SQL
helper such as `log_agent_action_to_db`.

## Where the schema lives

- `agent_mcp/db/schema.py::init_database()` — `CREATE TABLE IF NOT
  EXISTS` for every table. Run unconditionally at startup. **Owns
  CREATE TABLE for fresh DBs until each table has an ORM model +
  migration.**
- `agent_mcp/db/models/*.py` — declarative SQLAlchemy models. Each
  model file must mirror the column set + types that `init_database()`
  creates for the same table. CI catches drift via
  `tests/test_sqlalchemy_project_context.py::test_project_context_model_columns_match_raw_schema`.
- `migrations/versions/*.py` — Alembic revisions. Applied
  automatically on startup via
  `agent_mcp.db.migrations_runner.run_migrations_upgrade()`.

## Adding a new migration

```sh
cd <repo-root>
# Make sure MCP_PROJECT_DIR points at a scratch DB you don't mind
# mutating; alembic ini's URL is a placeholder.
export MCP_PROJECT_DIR=/tmp/agent-mcp-migration-scratch
mkdir -p "$MCP_PROJECT_DIR"

alembic revision -m "add foo column to bar"
# Edit the new file under migrations/versions/.
alembic upgrade head
```

Then commit both the model change and the migration in the same PR.
Application startup will pick up the new revision on the next boot;
no manual ops step needed.

## Adding a new ORM table (cutover pattern)

1. Add a model class under `agent_mcp/db/models/<table>.py` that
   mirrors what `init_database()` already creates.
2. Re-export it from `agent_mcp/db/models/__init__.py`.
3. Generate an Alembic revision — even if the on-disk schema already
   matches, the revision documents the cutover and gives future
   migrations a parent to chain off.
4. Convert the tool implementations and REST handlers that touch the
   table to use `SessionLocal()` instead of `get_db_connection()`. The
   `project_context_tools.py` rewrite in PR 7a is the reference.
5. Keep `init_database()` creating the table for now — remove the raw
   `CREATE TABLE` only after every code path has switched to the ORM.

## Branch labels

The baseline revision (`0001_baseline_initial_schema.py`) carries
`branch_labels=("agent_mcp",)`. This lets the fork's migrations co-
exist with hypothetical upstream migrations if rinadelph ever adopts
Alembic with its own root revision. New revisions inherit the branch
implicitly via their `down_revision` chain — only the root needs the
label.
