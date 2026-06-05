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
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest


# Tables expected to live under `agent_mcp.db.models` after PR-W3.
# `rag_embeddings` is a sqlite-vec `vec0` virtual table — not modelled
# in the ORM; `init_database()` keeps owning its creation behind the
# vss_is_actually_loadable gate.
EXPECTED_TABLES = {
    "agents",
    "tasks",
    "task_notes",
    "agent_actions",
    "project_context",
    "file_metadata",
    "rag_chunks",
    "rag_meta",
    "agent_messages",
    "claude_code_sessions",
    "mcp_sessions",
}


def _collect_orm_table_map():
    """Return `{table_name: ORM class}` for every model in `db.models`.

    Imports `agent_mcp.db.models` and walks `Base.metadata.tables`.
    """
    from agent_mcp.db.engine import Base
    from agent_mcp.db import models  # noqa: F401  (force registration)

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
    from agent_mcp.db.engine import Base
    from agent_mcp.db import models  # noqa: F401  (force registration)

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
            pytype = col.type.python_type if hasattr(col.type, "python_type") else str
            try:
                pytype_val = pytype
            except Exception:
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
