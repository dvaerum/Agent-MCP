"""Migration 0017 sanitizes legacy non-conforming memory keys to the
allowed charset (disallowed chars -> ``_``), collision-safe and
idempotent. On current data it is a no-op (0 offenders); this exercises
the correctness path against a seeded legacy DB.
"""

from __future__ import annotations

import importlib.util
import pathlib

import sqlalchemy as sa

from agent_mcp.utils.string_utils import is_valid_memory_key


def _load_migration():
    p = (
        pathlib.Path(__file__).resolve().parents[1]
        / "agent_mcp/migrations/versions/0017_sanitize_memory_keys.py"
    )
    spec = importlib.util.spec_from_file_location("_mig0017", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0017_sanitizes_collision_safe_and_idempotent(tmp_path) -> None:
    mig = _load_migration()
    eng = sa.create_engine(f"sqlite:///{tmp_path / 't.db'}")

    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE project_context ("
                "context_key TEXT PRIMARY KEY, value TEXT, "
                "updated_at TEXT, updated_by TEXT)"
            )
        )
        seed = [
            "ios/ok-key",     # conforming — must be untouched
            "bad key",        # space -> bad_key
            "ns:col",         # ':' -> ns_col, which COLLIDES with the next
            "ns_col",         # already conforming; the collision target
        ]
        for k in seed:
            conn.execute(
                sa.text(
                    "INSERT INTO project_context "
                    "(context_key, value, updated_at, updated_by) "
                    "VALUES (:k, 'v', 't', 'u')"
                ),
                {"k": k},
            )

    with eng.begin() as conn:
        renames = mig._sanitize_context_keys(conn)

    with eng.connect() as conn:
        keys = {
            r[0]
            for r in conn.execute(sa.text("SELECT context_key FROM project_context"))
        }

    # conforming keys untouched
    assert "ios/ok-key" in keys
    assert "ns_col" in keys
    # bad key sanitized
    assert "bad key" not in keys and "bad_key" in keys
    # collision-safe: 'ns:col' -> 'ns_col' collided, got a suffix
    assert "ns:col" not in keys and "ns_col_2" in keys
    # every key now valid; row count preserved (rename, not delete)
    assert len(keys) == 4
    assert all(is_valid_memory_key(k) for k in keys), keys
    assert set(dict(renames)) == {"bad key", "ns:col"}

    # idempotent: a second pass renames nothing
    with eng.begin() as conn:
        assert mig._sanitize_context_keys(conn) == []
