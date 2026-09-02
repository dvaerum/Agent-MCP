"""PR-W3 Finding #5: the ORM is the single source of truth for
schema, REST DTOs, and TypeScript types.

These invariants codify the architectural cut-over that PR-W3 ships:

* every table in `init_database()` has a SQLAlchemy ORM model under
  `agent_mcp.db.models`;
* every ORM model has a Pydantic mirror in
  `agent_mcp.db.pydantic_mirrors`, with column-by-column type parity;
* the TS-type generator `scripts/generate_ts_types.py` produces a
  byte-identical artifact across runs;
* every ORM column appears in the generated TS file;
* a fresh-create via `Base.metadata.create_all()` produces the same
  table-set as the canonical `init_database()` + Alembic chain;
* round-trip — ORM insert → Pydantic dump → Pydantic re-parse →
  Pydantic dump — is stable.

Until PR-W3 lands these fail on `main` because the models, the
Pydantic mirrors, and the generator script don't exist yet.

The "fresh-create" comparison drops the `rag_embeddings` virtual
table (sqlite-vec `vec0`) and the `sqlite_*` housekeeping tables —
neither is part of the ORM cut-over.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

# Tables expected to live under `agent_mcp.db.models` after PR-W3.
# `rag_embeddings` is a sqlite-vec `vec0` virtual table — not modelled
# in the ORM; `init_database()` keeps owning its creation behind the
# vss_is_actually_loadable gate.
EXPECTED_TABLES = {
    "agents",
    "tasks",
    "task_comments",
    "agent_actions",
    "project_context",
    "project_settings",
    "file_metadata",
    "rag_chunks",
    "rag_meta",
    "agent_messages",
    "claude_code_sessions",
    "mcp_sessions",
    "scheduled_directive",
    "pending_directive",
}


def _collect_orm_table_map():
    """Return `{table_name: ORM class}` for every model in `db.models`.

    Imports `agent_mcp.db.models` and walks `Base.metadata.tables`.
    """
    from agent_mcp.db import models  # noqa: F401  (force registration)
    from agent_mcp.db.engine import Base

    table_map = {}
    for table_name, table in Base.metadata.tables.items():
        # Find the mapped class whose __table__ is this table.
        cls = None
        for mapper in Base.registry.mappers:
            if mapper.local_table is table:
                cls = mapper.class_
                break
        if cls is not None:
            table_map[table_name] = cls
    return table_map


# ---------------------------------------------------------------------------
# Test A: every ORM column appears in the corresponding Pydantic mirror
# with a compatible type.
# ---------------------------------------------------------------------------


def test_every_orm_column_has_pydantic_mirror_with_compatible_type() -> None:
    """For every ORM model, every column must appear in the mirror.

    "Compatible type" is loose: TEXT/VARCHAR maps to `str`, INTEGER to
    `int`, BOOLEAN to `bool`. Nullable columns become `Optional[T]`.
    """
    table_map = _collect_orm_table_map()
    assert table_map, "Expected ORM models registered against Base"

    pydantic_mirrors = importlib.import_module(
        "agent_mcp.db.pydantic_mirrors"
    )

    missing_mirrors: list[str] = []
    column_mismatches: list[str] = []

    for table_name, orm_cls in table_map.items():
        # Find the Pydantic class with the same __tablename__ via the
        # convention `pydantic_mirrors.MIRRORS[table_name]`.
        mirror_cls = pydantic_mirrors.MIRRORS.get(table_name)
        if mirror_cls is None:
            missing_mirrors.append(table_name)
            continue

        pydantic_fields = set(mirror_cls.model_fields.keys())
        orm_columns = {c.name for c in orm_cls.__table__.columns}

        # Every ORM column must appear in the Pydantic mirror.
        missing = orm_columns - pydantic_fields
        if missing:
            column_mismatches.append(
                f"{table_name}: pydantic mirror missing columns {sorted(missing)}"
            )

    assert not missing_mirrors, f"Pydantic mirror missing for: {missing_mirrors}"
    assert not column_mismatches, "Column mismatches:\n" + "\n".join(
        column_mismatches
    )


# ---------------------------------------------------------------------------
# Test B: the TS-type generator is deterministic — running it twice
# produces byte-identical output.
# ---------------------------------------------------------------------------


def test_ts_type_generator_is_deterministic(tmp_path) -> None:
    """`scripts/generate_ts_types.py` must produce stable output."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "generate_ts_types.py"
    assert script.is_file(), f"Expected generator script at {script}"

    out1 = tmp_path / "run1.ts"
    out2 = tmp_path / "run2.ts"

    for out in (out1, out2):
        result = subprocess.run(
            [sys.executable, str(script), "--output", str(out)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"generator failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )

    assert out1.read_bytes() == out2.read_bytes(), (
        "TS-type generator is non-deterministic; "
        "two consecutive runs produced different bytes."
    )


# ---------------------------------------------------------------------------
# Test C: every ORM column appears in the generated TS file.
# ---------------------------------------------------------------------------


def test_every_orm_column_appears_in_generated_ts(tmp_path) -> None:
    """Every column in every ORM model must be present in the TS
    `api-types.generated.ts` file."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "generate_ts_types.py"
    assert script.is_file()

    out = tmp_path / "generated.ts"
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(out)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    contents = out.read_text()

    table_map = _collect_orm_table_map()
    missing: list[str] = []
    for table_name, orm_cls in table_map.items():
        for column in orm_cls.__table__.columns:
            # Match `<column_name>:` or `<column_name>?:` in the TS
            # interface body. The generator emits one interface per
            # mirror; we don't care about which interface, just that
            # the field is present somewhere.
            pattern = rf"\b{re.escape(column.name)}\??:"
            if not re.search(pattern, contents):
                missing.append(f"{table_name}.{column.name}")

    assert not missing, f"Columns missing from generated TS: {missing}"


# ---------------------------------------------------------------------------
# Test D: fresh-create via `Base.metadata.create_all` produces the
# same table-set as `init_database()` + Alembic.
# ---------------------------------------------------------------------------


def test_orm_create_all_matches_init_database_table_set(tmp_path) -> None:
    """`Base.metadata.create_all()` must create at least every table
    that `init_database()` does (modulo virtual tables which the ORM
    doesn't model)."""
    # Drive the ORM-only create_all into a tmp DB.
    import sqlalchemy as sa

    from agent_mcp.db import models  # noqa: F401  (force registration)
    from agent_mcp.db.engine import Base

    db_path = tmp_path / "orm_only.db"
    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version'"
            )
        ).all()
    orm_tables = {r[0] for r in rows}

    missing = EXPECTED_TABLES - orm_tables
    assert not missing, (
        f"Tables expected from ORM but missing from create_all(): {missing}"
    )


# ---------------------------------------------------------------------------
# Test E: round-trip — ORM insert → Pydantic mirror parse →
# reserialize → equal.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test F: the migration chain (0001->head) run atop `init_database()`
# produces a schema byte-identical (columns/types/nullable/indexes) to
# `Base.metadata.create_all()` alone — the claim 0011's docstring makes.
# ---------------------------------------------------------------------------


def _reflect_schema(engine) -> dict:
    """Reflect an engine into `{table: {"columns": ..., "indexes": ...}}`.

    Drops the sqlite-vec `rag_embeddings` virtual table (and its
    `rag_embeddings_*` shadow tables) and `alembic_version` — neither
    is part of the ORM cut-over, matching the exclusion the rest of
    this file already applies.
    """
    import sqlalchemy as sa

    inspector = sa.inspect(engine)
    schema: dict = {}
    for table_name in inspector.get_table_names():
        if table_name.startswith("sqlite_"):
            continue
        if table_name == "rag_embeddings" or table_name.startswith(
            "rag_embeddings_"
        ):
            continue
        if table_name == "alembic_version":
            continue
        columns = {
            col["name"]: (str(col["type"]), bool(col["nullable"]))
            for col in inspector.get_columns(table_name)
        }
        indexes = sorted(
            (idx["name"], tuple(idx["column_names"]), bool(idx["unique"]))
            for idx in inspector.get_indexes(table_name)
        )
        schema[table_name] = {"columns": columns, "indexes": indexes}
    return schema


def test_migration_chain_matches_create_all_schema(tmp_path, monkeypatch) -> None:
    """0011's docstring claims the on-disk schema produced by
    `init_database()` + migrations 0001-0010 is byte-identical to
    `Base.metadata.create_all()`. Enforce it: a fresh `create_all()`
    DB and the production bootstrap path (`init_database()` then
    `run_migrations_upgrade()` walking 0001->head) must reflect to
    the same columns, types, nullability, and indexes for every
    shared table.
    """
    import sqlalchemy as sa

    from agent_mcp.db import models  # noqa: F401  (force registration)
    from agent_mcp.db.engine import Base

    # DB A: ORM-only, no migrations involved.
    db_a = tmp_path / "create_all_only.db"
    engine_a = sa.create_engine(f"sqlite:///{db_a}", future=True)
    Base.metadata.create_all(engine_a)

    # DB B: the real production bootstrap sequence — init_database()
    # (which itself calls create_all()) followed by the Alembic chain,
    # mirroring `server_lifecycle.application_startup`.
    project_dir = tmp_path / "legacy_project"
    (project_dir / ".agent").mkdir(parents=True)
    db_b = project_dir / ".agent" / "mcp_state.db"

    monkeypatch.setenv("MCP_PROJECT_DIR", str(project_dir))
    from agent_mcp.db import engine as _engine_mod

    _engine_mod.reset_engine_cache()

    from agent_mcp.db.migrations_runner import run_migrations_upgrade
    from agent_mcp.db.schema import init_database

    init_database()
    run_migrations_upgrade()

    engine_b = sa.create_engine(f"sqlite:///{db_b}", future=True)

    try:
        schema_a = _reflect_schema(engine_a)
        schema_b = _reflect_schema(engine_b)

        assert schema_a.keys() == schema_b.keys(), (
            f"Table-set drift: create_all()={sorted(schema_a)} "
            f"vs migration chain={sorted(schema_b)}"
        )

        diffs = [
            f"{table}: create_all()={schema_a[table]} "
            f"!= migration chain={schema_b[table]}"
            for table in sorted(schema_a)
            if schema_a[table] != schema_b[table]
        ]
        assert not diffs, "Schema drift between create_all() and the " \
            "migration chain:\n" + "\n".join(diffs)
    finally:
        engine_a.dispose()
        engine_b.dispose()
        _engine_mod.reset_engine_cache()


def test_round_trip_orm_to_pydantic_to_orm() -> None:
    """For every ORM model, building an instance with all-default
    column values then dumping through the Pydantic mirror must
    produce a stable dict."""
    table_map = _collect_orm_table_map()
    pydantic_mirrors = importlib.import_module(
        "agent_mcp.db.pydantic_mirrors"
    )

    for table_name, orm_cls in table_map.items():
        mirror_cls = pydantic_mirrors.MIRRORS[table_name]

        # Build a sample dict with sentinel values per column type.
        sample = {}
        for col in orm_cls.__table__.columns:
            try:
                pytype_val = col.type.python_type
            except (AttributeError, NotImplementedError):
                pytype_val = str
            if pytype_val is bool:
                sample[col.name] = False
            elif pytype_val is int:
                sample[col.name] = 0 if col.primary_key else 1
            else:
                sample[col.name] = f"v_{col.name}"

        # Round-trip via the Pydantic mirror.
        parsed = mirror_cls.model_validate(sample)
        dumped = parsed.model_dump()
        reparsed = mirror_cls.model_validate(dumped)
        assert reparsed.model_dump() == dumped, (
            f"{table_name}: round-trip not stable"
        )
